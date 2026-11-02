"""Consumer-owned native probe validation; no benchmark model imports."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from python_fixtures import python_document, python_attempt
from viewer.errors import ViewerError
from viewer.models.evidence import ArtifactReceipt
from viewer.models.python_runtime import PythonProbe
from viewer.validation.python_evidence import validate_python_attempt


def native_probe() -> dict:
    return {'schema_version': 2, 'version': '3.12.0', 'executable': '@attempt/home/.venv/bin/python',
            'prefix': '@attempt/home/.venv', 'base_prefix': '@runtime/python'}


def test_legacy_probe_roundtrip_preserves_absent_version() -> None:
    old = python_attempt({})['before_setup']
    assert PythonProbe.model_validate(old).to_wire() == old
    assert PythonProbe.model_validate(native_probe()).to_wire() == native_probe()


@pytest.mark.parametrize('change', [
    {'schema_version': 3}, {'schema_version': 1}, {'prefix': '/sandbox-home/.venv'},
    {'executable': '/Users/private/python'}, {'base_prefix': '/benchmark-python'},
])
def test_consumer_rejects_invalid_native_path_contract(change: dict) -> None:
    with pytest.raises(ValidationError):
        PythonProbe.model_validate({**native_probe(), **change})


@pytest.mark.parametrize(('platform', 'version', 'accepted'), [
    ('darwin-arm64', 2, True), ('darwin-x86_64', 2, True), ('linux-x86_64', 1, True),
    ('darwin-arm64', 1, False), ('linux-x86_64', 2, False),
])
def test_consumer_binds_probe_version_to_runtime(platform: str, version: int, accepted: bool) -> None:
    runtime = ArtifactReceipt(path='_meta/python.json', sha256='a' * 64, size_bytes=1)
    prefix = 'tasks/001/attempts/001/'
    receipt = ArtifactReceipt(path=prefix + 'python.json', sha256='b' * 64, size_bytes=1)
    base = {**python_document(), 'platform': platform}
    probe = native_probe() if version == 2 else python_attempt({})['before_setup']
    documents = {
        runtime.path: base,
        receipt.path: {'runtime': runtime.to_wire(), 'before_setup': probe, 'before_agent': probe},
    }
    fact = {'python_runtime': runtime.to_wire(), 'artifact_receipts': [runtime.to_wire(), receipt.to_wire()]}
    store = SimpleNamespace(
        registry=SimpleNamespace(schema_version=2), state=SimpleNamespace(python_runtime=runtime),
        inventory={runtime.path: runtime, receipt.path: receipt},
        document=lambda path, model: model.model_validate(documents[path]),
    )
    seal = SimpleNamespace(
        to_wire=lambda: fact,
        task=SimpleNamespace(execution=SimpleNamespace(status='completed'), setup=SimpleNamespace(status='skipped')),
    )
    if accepted:
        validate_python_attempt(seal, store, prefix)
    else:
        with pytest.raises(ViewerError):
            validate_python_attempt(seal, store, prefix)
