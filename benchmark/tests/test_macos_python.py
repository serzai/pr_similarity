"""Mach-O dependency resolution and real package-free native Python isolation."""

import json
import platform
import subprocess
import threading
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.macos_python import dependencies, resolve_library
from benchmark.execution import macos_python
from benchmark.execution.python_runtime import PythonRuntime, prepare_python
from benchmark.execution.seatbelt import SeatbeltSandbox
from benchmark.execution.opencode import Invocation
from benchmark.models.runtime import PythonAttemptReceipt
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.storage import write_json


def test_universal_dependency_list_is_unique_and_preserves_spaces() -> None:
    assert dependencies([
        'Python (architecture arm64):',
        '\t/Library/My Python/Python (compatibility version 3.12.0, current version 3.12.0)',
        '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)',
        'Python (architecture x86_64):',
        '\t/Library/My Python/Python (compatibility version 3.12.0, current version 3.12.0)',
    ]) == ['/Library/My Python/Python', '/usr/lib/libSystem.B.dylib']


@pytest.mark.parametrize('token', ['absolute', 'loader', 'executable', 'rpath'])
def test_resolves_native_library_tokens(tmp_path: Path, token: str) -> None:
    (tmp_path / 'bin').mkdir()
    library = tmp_path / 'lib/libprivate.dylib'
    library.parent.mkdir()
    library.write_bytes(b'fixture')
    names = {
        'absolute': str(library), 'loader': '@loader_path/../lib/libprivate.dylib',
        'executable': '@executable_path/../lib/libprivate.dylib', 'rpath': '@rpath/libprivate.dylib',
    }
    actual = resolve_library(
        names[token], tmp_path / 'bin/extension.so', tmp_path / 'bin/python', [str(library.parent)]
    )
    assert actual == library.resolve()


def test_ambiguous_rpath_is_not_silently_resolved_to_a_host_library(tmp_path: Path) -> None:
    for directory in ('one', 'two'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'library.dylib').write_bytes(b'fixture')
    with pytest.raises(BenchmarkError) as error:
        resolve_library('@rpath/library.dylib', tmp_path / 'bin/python', tmp_path / 'bin/python',
                        [str(tmp_path / 'one'), str(tmp_path / 'two')])
    assert error.value.code == 'python_library_missing'


def test_rpath_tokens_are_anchored_to_their_own_load_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(macos_python, '_tool', lambda *_args: [
        '          cmd LC_RPATH', '         path @loader_path/../private (offset 12)',
        '          cmd LC_RPATH', '         path @executable_path/../shared (offset 12)',
        '          cmd LC_RPATH', '         path @loader_path (offset 12)',
        '          cmd LC_RPATH', '         path @executable_path (offset 12)',
    ])
    actual = macos_python._rpaths(tmp_path / 'extensions/module.so', tmp_path / 'bin/python', tmp_path, None)
    assert set(actual) == {
        str(tmp_path / 'extensions/../private'), str(tmp_path / 'bin/../shared'),
        str(tmp_path / 'extensions'), str(tmp_path / 'bin'),
    }


native = pytest.mark.skipif(platform.system() != 'Darwin', reason='macOS Python integration')


@pytest.fixture(scope='module')
def native_python(tmp_path_factory: pytest.TempPathFactory) -> PythonRuntime:
    return prepare_python(tmp_path_factory.mktemp('native-python-cache'))


@pytest.fixture
def sandbox(tmp_path: Path, native_python: PythonRuntime) -> SeatbeltSandbox:
    root = tmp_path / 'attempt Мак " (native)'
    root.mkdir()
    source = root / 'source'
    source.mkdir()
    return SeatbeltSandbox('/usr/bin/sandbox-exec', source, source, root / 'home',
                           root / 'runtime', Path('/usr/bin/true'), native_python)


def invoke(sandbox: SeatbeltSandbox, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(sandbox.command(arguments), env=sandbox.environment({}), cwd=sandbox.source,
                          capture_output=True, text=True, timeout=30, check=False)


@native
def test_new_native_attempt_bootstraps_without_a_reused_preflight_home(sandbox: SeatbeltSandbox) -> None:
    """Per-attempt setup does not depend on the coordinator's disposable probe directories."""
    assert Path(sandbox.paths.temporary).is_dir()
    assert sandbox.prepare_python().schema_version == 2


@native
def test_private_python_probe_and_package_install(sandbox: SeatbeltSandbox) -> None:
    sandbox.preflight()
    probe = sandbox.prepare_python()
    assert probe.schema_version == 2
    assert probe.prefix == '@attempt/home/.venv'
    assert str(sandbox.home) not in json.dumps(probe.to_wire())
    assert str(sandbox.home) not in (sandbox.runtime_files / 'python-check.log').read_text()
    assert str(sandbox.python.root) not in (sandbox.runtime_files / 'python-check.log').read_text()
    check = invoke(sandbox, ['python', '-c',
        'import importlib.util,json;print(json.dumps([importlib.util.find_spec(name) is None '
        'for name in ("pydantic","pytest","httpx")]))'])
    assert check.returncode == 0, check.stderr
    assert json.loads(check.stdout) == [True, True, True]
    wheel = sandbox.home / 'native_fixture-0.0.1-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('native_fixture.py', 'VALUE = 42\n')
        archive.writestr(
            'native_fixture-0.0.1.dist-info/METADATA',
            'Metadata-Version: 2.1\nName: native-fixture\nVersion: 0.0.1\n',
        )
        archive.writestr(
            'native_fixture-0.0.1.dist-info/WHEEL',
            'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        )
        archive.writestr('native_fixture-0.0.1.dist-info/RECORD', '')
    result = invoke(sandbox, ['python', '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel)])
    assert result.returncode == 0, result.stderr
    result = invoke(sandbox, ['python3.12', '-c', 'import native_fixture;print(native_fixture.VALUE)'])
    assert result.returncode == 0 and result.stdout.strip() == '42', result.stderr
    assert sandbox.check_python() == probe
    assert not (sandbox.python.root / 'lib/python3.12/site-packages').exists()


@native
def test_private_python_loads_system_trust_without_host_framework_access(sandbox: SeatbeltSandbox) -> None:
    sandbox.prepare_python()
    result = invoke(sandbox, ['python', '-c',
        'import json,ssl;ctx=ssl.create_default_context();'
        'print(json.dumps([len(ctx.get_ca_certs()),ctx.verify_mode,ctx.check_hostname,'
        'ssl.get_default_verify_paths().cafile]))'])
    assert result.returncode == 0, result.stderr
    count, mode, hostname, bundle = json.loads(result.stdout)
    assert count > 0
    assert mode == 2 and hostname is True  # CERT_REQUIRED, with hostname verification.
    assert bundle == '/private/etc/ssl/cert.pem'
    private_ca = str(sandbox.home / 'project-ca.pem')
    assert sandbox.environment({'SSL_CERT_FILE': private_ca})['SSL_CERT_FILE'] == private_ca


@native
def test_native_attempts_do_not_share_site_packages(sandbox: SeatbeltSandbox, tmp_path: Path) -> None:
    sandbox.preflight(); sandbox.prepare_python()
    first = invoke(sandbox, ['python', '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'])
    site = Path(first.stdout.strip())
    (site / 'attempt_marker.py').write_text('VALUE=42\n')
    root = tmp_path / 'second'; root.mkdir()
    source = root / 'source'; source.mkdir()
    second = replace(sandbox, source=source, cwd=source, home=root / 'home', runtime_files=root / 'runtime')
    second.preflight(); second.prepare_python()
    result = invoke(second, ['python', '-c', 'import importlib.util;print(importlib.util.find_spec("attempt_marker"))'])
    assert result.returncode == 0 and result.stdout.strip() == 'None', result.stderr


@native
def test_native_probe_ignores_source_shadow_modules(sandbox: SeatbeltSandbox) -> None:
    (sandbox.source / 'ssl.py').write_text('raise RuntimeError("source shadow module")\n')
    (sandbox.source / 'project_marker.py').write_text('VALUE=42\n')
    sandbox.preflight(); sandbox.prepare_python()
    result = invoke(sandbox, ['python', '-c', 'import project_marker;print(project_marker.VALUE)'])
    assert result.returncode == 0 and result.stdout.strip() == '42', result.stderr
    assert sandbox.check_python().schema_version == 2
    direct = invoke(sandbox, ['/usr/bin/python3', '-c', 'print("host")'])
    assert direct.returncode != 0


@native
def test_native_snapshot_is_reproducible_and_has_no_unmeasured_bytecode(
    native_python: PythonRuntime
) -> None:
    repeated = prepare_python(native_python.root.parent.parent)
    assert repeated.receipt == native_python.receipt
    assert repeated.root == native_python.root
    assert not list(native_python.root.rglob('__pycache__'))
    origin = json.loads((native_python.root / 'lib/prsim-python-origin.json').read_text())
    assert origin['preparation'] == 'macho-relocation-v1'
    assert 'bin/python3.12' in origin['source_sha256']


@native
def test_cancelled_native_python_preparation_does_not_publish(tmp_path: Path) -> None:
    stop = threading.Event(); stop.set()
    with pytest.raises(BenchmarkError) as error:
        prepare_python(tmp_path, cancel=stop)
    assert error.value.code == 'operation_cancelled'
    assert not (tmp_path / 'python').exists()


@native
def test_native_python_setup_agent_and_durable_probe(sandbox: SeatbeltSandbox, tmp_path: Path) -> None:
    """Compose the native Python environment with the real setup/agent launcher."""
    binary = tmp_path / 'fixture-opencode'
    binary.write_text(
        '#!/usr/bin/env python\n'
        'import json,os,sys,ssl,sqlite3\n'
        'assert sys.prefix == os.environ["VIRTUAL_ENV"]\n'
        'if sys.argv[1] == "debug":\n'
        ' print(os.environ["OPENCODE_CONFIG_CONTENT"]);sys.exit(0)\n'
        'sys.stdin.read()\n'
        'open("result.py","w").write("VALUE=42\\n")\n'
        'print(json.dumps({"type":"text","sessionID":"native-python-fixture",'
        '"part":{"type":"text","text":"done"}}))\n'
    )
    binary.chmod(0o700)
    sandbox = replace(sandbox, binary=binary)
    sandbox.preflight()
    before = sandbox.prepare_python()
    setup = tmp_path / 'setup.sh'
    setup.write_text('python -c \'import sys; assert sys.prefix.endswith("/.venv")\'\n')
    config_file = tmp_path / 'config.yaml'
    config_file.write_text(yaml.safe_dump({
        'schema_version': 1,
        'agent': {'name': 'native-python', 'runtime': 'opencode:v1.18.33',
                  'config': {'opencode_json': {'model': 'fixture/model'}},
                  'setup': {'script': str(setup), 'timeout_seconds': 15}},
    }))
    artifacts = tmp_path / 'artifacts'
    invocation = Invocation(load_config(config_file), sandbox, artifacts, artifacts / 'logs')
    prompt = invocation.prepare('Offline native Python task')
    result = invocation.setup(threading.Event())
    assert result is not None and result.status == 'completed'
    after = sandbox.check_python()
    assert invocation.dump_effective_config(threading.Event()).status == 'available'
    result, tracker = invocation.run(prompt, threading.Event())
    assert result.status == 'completed' and tracker.root_session_id == 'native-python-fixture'
    assert (sandbox.source / 'result.py').read_text() == 'VALUE=42\n'
    write_json(tmp_path / '_meta/python.json', sandbox.python.receipt)
    write_json(artifacts / 'python.json', PythonAttemptReceipt(
        runtime=reference(tmp_path, '_meta/python.json'), before_setup=before, before_agent=after,
    ))
    assert str(tmp_path) not in (artifacts / 'python.json').read_text()
