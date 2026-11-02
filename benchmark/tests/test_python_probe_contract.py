"""Versioned native probe paths retain historical Linux reads without host paths."""

from types import SimpleNamespace
from pathlib import Path
import threading

import pytest
from pydantic import ValidationError
from benchmark.errors import BenchmarkError
from benchmark.models.runtime import PythonProbe
from benchmark.models.runtime import ProvisioningReceipt
from benchmark.execution.provisioning import Runtime
from benchmark.execution.python_runtime import PythonRuntime
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.python_evidence import validate_python_fact
from benchmark.persistence.storage import write_json
from python_fixtures import python_probe, python_receipt
from benchmark.runtime import coordinator


def native_probe() -> dict:
    return {'schema_version': 2, 'version': '3.12.0', 'executable': '@attempt/home/.venv/bin/python',
            'prefix': '@attempt/home/.venv', 'base_prefix': '@runtime/python'}


def test_old_probe_bytes_do_not_gain_a_version_field() -> None:
    old = python_probe().to_wire()
    assert 'schema_version' not in old
    assert PythonProbe.model_validate(old).to_wire() == old
    assert PythonProbe.model_validate(native_probe()).to_wire() == native_probe()


@pytest.mark.parametrize('change', [
    {'schema_version': 3}, {'schema_version': 1}, {'prefix': '/sandbox-home/.venv'},
    {'executable': '/Users/private/python'}, {'base_prefix': '/benchmark-python'},
])
def test_native_probe_rejects_mixed_or_unversioned_paths(change: dict) -> None:
    with pytest.raises(ValidationError):
        PythonProbe.model_validate({**native_probe(), **change})


@pytest.mark.parametrize(('platform', 'probe_version', 'accepted'), [
    ('darwin-arm64', 2, True), ('darwin-x86_64', 2, True), ('linux-x86_64', 1, True),
    ('darwin-arm64', 1, False), ('linux-x86_64', 2, False),
])
def test_sealed_probe_contract_matches_runtime_platform(
    tmp_path: Path, platform: str, probe_version: int, accepted: bool
) -> None:
    base = python_receipt().model_copy(update={'platform': platform})
    write_json(tmp_path / '_meta/python.json', base)
    runtime = reference(tmp_path, '_meta/python.json')
    probe = native_probe() if probe_version == 2 else python_probe().to_wire()
    prefix = 'tasks/001/attempts/001/'
    write_json(tmp_path / (prefix + 'python.json'), {
        'runtime': runtime.to_wire(), 'before_setup': probe, 'before_agent': probe,
    })
    receipt = reference(tmp_path, prefix + 'python.json')
    fact = SimpleNamespace(
        python_runtime=runtime, artifact_receipts=[runtime, receipt],
        task=SimpleNamespace(execution=SimpleNamespace(status='completed'), setup=SimpleNamespace(status='skipped')),
    )
    if accepted:
        validate_python_fact(tmp_path, fact, runtime, prefix)
    else:
        with pytest.raises(BenchmarkError) as error:
            validate_python_fact(tmp_path, fact, runtime, prefix)
        assert error.value.code == 'python_probe_mismatch'


def test_resume_rejects_platform_change_even_when_python_bytes_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = python_receipt().model_copy(update={'platform': 'linux-x86_64'})
    write_json(tmp_path / '_meta/python.json', previous)
    selected = reference(tmp_path, '_meta/python.json')
    current = previous.model_copy(update={'platform': 'darwin-arm64'})
    runtime = Runtime(Path('/usr/bin/true'), ProvisioningReceipt(
        selector='opencode:v1.18.33', actual_version='1.18.33', asset_name='fixture', platform='fixture',
        archive_sha256='a' * 64, binary_sha256='b' * 64,
    ))
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(coordinator, 'prepare_python', lambda *_args, **_kwargs: PythonRuntime(tmp_path, current))
    owner = SimpleNamespace(
        config=SimpleNamespace(credential_environment=lambda: {}, data=SimpleNamespace(
            agent=SimpleNamespace(runtime='opencode:v1.18.33'),
            provisioning=SimpleNamespace(cache_dir=str(tmp_path / 'cache'), download_timeout_seconds=1),
        )),
        command=SimpleNamespace(phase=lambda *_args: None), stop=threading.Event(), run_dir=tmp_path,
        store=SimpleNamespace(state=SimpleNamespace(resolved_runtime=None, python_runtime=selected)),
    )
    with pytest.raises(BenchmarkError) as error:
        coordinator.Coordinator.prepare_agents(owner)
    assert error.value.code == 'resume_python_mismatch'
