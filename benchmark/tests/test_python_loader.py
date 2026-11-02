"""Custom Linux ELF snapshots must run their frozen Python, not the host libpython."""

import hashlib
import json
from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.execution.python_runtime import PythonRuntime
from benchmark.execution.sandbox import Sandbox
from benchmark.models.runtime import PythonFile, PythonReceipt
from python_fixtures import python_probe, python_receipt


def runtime_fixture(root: Path, *, platform: str, private: bool) -> PythonRuntime:
    receipt = python_receipt()
    files = list(receipt.files)
    if private:
        files.append(PythonFile(path='lib/libpython3.12.so.1.0', sha256='b' * 64, size_bytes=1))
    signature = hashlib.sha256(''.join(f'{item.path}\0{item.sha256}\0{item.size_bytes}\n' for item in files).encode())
    data = {**receipt.to_wire(), 'files': [item.to_wire() for item in files],
            'platform': platform, 'content_sha256': signature.hexdigest()}
    return PythonRuntime(root, PythonReceipt.model_validate(data))


@pytest.mark.parametrize('platform', ['linux-x86_64', 'darwin-arm64'])
@pytest.mark.parametrize('private', [False, True])
def test_only_private_linux_libraries_change_the_loader_environment(
    tmp_path: Path, platform: str, private: bool
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    runtime = runtime_fixture(tmp_path / 'python', platform=platform, private=private)
    sandbox = Sandbox('bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/bin/sh'), runtime)
    environment = sandbox.environment({})
    if private and platform.startswith('linux-'):
        assert environment['LD_LIBRARY_PATH'] == '/benchmark-python/lib'
    else:
        assert 'LD_LIBRARY_PATH' not in environment
    with pytest.raises(BenchmarkError, match='Bindings must not replace'):
        sandbox.environment({'LD_LIBRARY_PATH': '/host-library'})


@pytest.mark.parametrize('change', ['patch-version', 'location-contract'])
def test_linux_probe_mismatch_stops_during_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    runtime = runtime_fixture(tmp_path / 'python', platform='linux-x86_64', private=True)
    sandbox = Sandbox('bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/bin/sh'), runtime)
    if change == 'patch-version':
        probe = {**python_probe().to_wire(), 'version': '3.12.9'}
    else:
        probe = {'schema_version': 2, 'version': '3.12.0', 'executable': '@attempt/home/.venv/bin/python',
                 'prefix': '@attempt/home/.venv', 'base_prefix': '@runtime/python'}
    monkeypatch.setattr(Sandbox, '_python_process', lambda *_args, **_kwargs: [json.dumps(probe)])
    with pytest.raises(BenchmarkError) as error:
        sandbox.check_python()
    assert error.value.code == 'python_preflight_failed'


def test_elf_discovery_matches_setup_loader_without_exporting_preload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark.execution import python_runtime
    from benchmark.execution.process import ProcessResult

    selected = tmp_path / 'host-libraries'
    monkeypatch.setenv('LD_LIBRARY_PATH', str(selected))
    monkeypatch.setenv('LD_PRELOAD', 'HOST_PRELOAD_SENTINEL')
    binary = tmp_path / 'python3.12'

    def inspect(argv: list[str], **settings: object) -> ProcessResult:
        assert argv == ['ldd', str(binary)]
        assert settings['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LD_LIBRARY_PATH': str(selected)}
        assert settings['cwd'] == Path.cwd()
        output = settings['on_stdout']
        assert callable(output)
        output(f'libpython3.12.so.1.0 => {selected}/libpython3.12.so.1.0 (0x00000001)')
        output('libprivate.so.1 => /usr/local/lib/libprivate.so.1 (0x00000002)')
        output('libc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x00000003)')
        return ProcessResult('completed', 0, .01)

    monkeypatch.setattr(python_runtime, 'run_process', inspect)
    assert python_runtime._libraries({'bin/python3.12': binary}, None) == {
        'lib/libpython3.12.so.1.0': selected / 'libpython3.12.so.1.0',
        'lib/libprivate.so.1': Path('/usr/local/lib/libprivate.so.1').resolve(),
    }


def test_setup_bootstrap_retains_only_the_host_loader_selection(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark.execution import python_runtime
    from benchmark.execution.process import ProcessResult

    monkeypatch.setattr(python_runtime.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(python_runtime, 'discover_python', lambda: None)
    monkeypatch.setenv('LD_LIBRARY_PATH', '/opt/setup-python/lib')
    monkeypatch.setenv('LD_PRELOAD', 'HOST_PRELOAD_SENTINEL')
    observed: list[dict[str, str]] = []

    def bootstrap(_argv: list[str], **settings: object) -> ProcessResult:
        environment = settings['env']
        assert isinstance(environment, dict)
        observed.append(environment)
        return ProcessResult('completed', 0, .01)

    monkeypatch.setattr(python_runtime, 'run_process', bootstrap)
    python_runtime.validate_setup_python()
    assert len(observed) == 2
    assert all(environment['LD_LIBRARY_PATH'] == '/opt/setup-python/lib' for environment in observed)
    assert all('LD_PRELOAD' not in environment for environment in observed)
