"""Portable launcher resources and native process paths without model access."""

import json
import os
import platform
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml
from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.agent_input import freeze_agent_input, portable_native
from benchmark.execution.opencode import Invocation
from benchmark.execution.paths import ExecutionPaths
from benchmark.execution.process import ProcessResult, run_process
from benchmark.execution.seatbelt import SeatbeltSandbox
from benchmark.persistence.storage import read_json


def test_linux_paths_preserve_workspace_namespace(tmp_path: Path) -> None:
    """Linux paths remain stable even if the checkout and workspace move on the host."""
    paths = ExecutionPaths.bubblewrap(tmp_path / 'source', tmp_path / 'source/subproject')
    assert paths.cwd == '/repo/subproject'
    assert paths.source == '/repo'
    assert paths.runtime_file('resources/one/rules.md') == '/benchmark-runtime/resources/one/rules.md'
    assert paths.binary == '/benchmark-bin/opencode'


@pytest.mark.parametrize('relative', ['../escape', '/absolute', 'a//b', '', './file'])
def test_runtime_file_rejects_nonrelative_names(relative: str, tmp_path: Path) -> None:
    paths = ExecutionPaths.bubblewrap(tmp_path, tmp_path)
    with pytest.raises(ValueError):
        paths.runtime_file(relative)


def test_native_paths_reject_a_cwd_outside_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ExecutionPaths.native(
            tmp_path / 'source', tmp_path, tmp_path / 'home', tmp_path / 'runtime', tmp_path / 'bin', tmp_path / 'tmp'
        )


def test_portable_plugin_and_instruction_paths_do_not_capture_native_root(tmp_path: Path) -> None:
    root = str((tmp_path / 'runtime Мак').resolve())
    native = {
        'plugin': ['file://' + root + '/resources/1-0-0/plugin.js'],
        'instructions': [root + '/resources/0-0-0/rules.md'],
        'skills': {'paths': [root + '/resources/2-0-0/tdd']},
        'unrelated': root + '/something',
    }
    portable = portable_native(native, runtime_root=root)
    assert portable['plugin'] == ['file://resources/1-0-0/plugin.js']
    assert portable['instructions'] == ['resources/0-0-0/rules.md']
    assert portable['skills'] == {'paths': ['resources/2-0-0/tdd']}
    assert portable['unrelated'] == native['unrelated']
    assert native['instructions'] == [root + '/resources/0-0-0/rules.md']


@pytest.fixture
def sandbox(tmp_path: Path) -> SeatbeltSandbox:
    root = tmp_path / 'native Мак " (deny default)'
    source = root / 'source'
    (source / 'workspace').mkdir(parents=True)
    binary = root / 'fake-opencode'
    binary.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = debug ]; then printf "%s\\n" "$OPENCODE_CONFIG_CONTENT"; exit 0; fi\n'
        'cat > "$HOME/prompt"\n'
        'echo edited > result.txt\n'
        'printf \'{"type":"text","sessionID":"fixture","part":{"type":"text","text":"done"}}\\n\'\n'
    )
    binary.chmod(0o700)
    return SeatbeltSandbox(
        '/usr/bin/sandbox-exec', source, source / 'workspace', root / 'home', root / 'runtime', binary
    )


def test_native_environment_rejects_loader_injection(
    sandbox: SeatbeltSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ('DYLD_INSERT_LIBRARIES', '__CF_USER_TEXT_ENCODING', 'PATH', 'PYTHONPATH'):
        with pytest.raises(BenchmarkError):
            sandbox.environment({key: 'unsafe'})
    monkeypatch.setenv('PRSIM_HOST_SECRET', 'host-only')
    environment = sandbox.environment({'FIXTURE_KEY': 'secret'})
    assert environment['HOME'] == str(sandbox.home.resolve())
    assert environment['XDG_DATA_HOME'] == str(sandbox.home.resolve() / 'data')
    assert environment['FIXTURE_KEY'] == 'secret'
    assert 'PRSIM_HOST_SECRET' not in environment
    assert 'secret' not in sandbox.policy().profile()
    assert 'VIRTUAL_ENV' not in environment


def test_native_python_is_not_silently_replaced_by_host_python(sandbox: SeatbeltSandbox) -> None:
    for operation in (sandbox.prepare_python, sandbox.check_python):
        with pytest.raises(BenchmarkError) as error:
            operation()
        assert error.value.code == 'python_layout_unsupported'


def test_unavailable_seatbelt_never_falls_back_to_a_bare_command(
    sandbox: SeatbeltSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('benchmark.execution.seatbelt.platform.system', lambda: 'Darwin')
    calls = []

    def unavailable(argv: list[str], **_kwargs: object) -> ProcessResult:
        calls.append(argv)
        return ProcessResult('error', 71, 0.01)

    monkeypatch.setattr('benchmark.execution.seatbelt.run_process', unavailable)
    with pytest.raises(BenchmarkError) as error:
        sandbox.preflight()
    assert error.value.code == 'sandbox_unavailable'
    assert len(calls) == 1 and calls[0][0] == '/usr/bin/sandbox-exec'
    assert not (sandbox.runtime_files.parent / 'seatbelt-preflight-private').exists()


def test_invalid_native_layout_has_no_directory_side_effects(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    home = source / 'home'
    with pytest.raises(BenchmarkError):
        SeatbeltSandbox('/usr/bin/sandbox-exec', source, source, home, tmp_path / 'runtime', Path('/bin/sh'))
    assert not home.exists()


native = pytest.mark.skipif(platform.system() != 'Darwin', reason='macOS Seatbelt integration')


@native
@pytest.mark.parametrize('frozen', [False, True])
def test_native_setup_and_fake_agent_use_backend_paths(
    tmp_path: Path, sandbox: SeatbeltSandbox, frozen: bool
) -> None:
    """Exercise resource relocation, cwd, setup and native events under real Seatbelt."""
    setup = tmp_path / 'setup.sh'
    setup.write_text('pwd > working-directory; echo configured > "$HOME/tool"\n')
    rules = tmp_path / 'rules.md'
    rules.write_text('Fixture instructions')
    config_file = tmp_path / 'config.yaml'
    config_file.write_text(yaml.safe_dump({
        'schema_version': 1,
        'agent': {
            'name': 'native-fixture', 'runtime': 'opencode:v1.18.33',
            'config': {'opencode_json': {'model': 'fixture/model', 'instructions': [str(rules)]}},
            'setup': {'script': str(setup), 'timeout_seconds': 5},
        },
    }))
    config = load_config(config_file)
    assert sandbox.preflight().backend == 'seatbelt'
    run = tmp_path / 'run'
    run.mkdir()
    selected = freeze_agent_input(run, config) if frozen else None
    invocation = Invocation(
        config, sandbox, run / 'artifacts', run / 'logs',
        run_directory=run if frozen else None, agent_input=selected,
    )
    prompt = invocation.prepare('Fixture task')
    native_config = read_json(sandbox.runtime_files / 'config.json')
    assert native_config['instructions'] == [sandbox.paths.runtime + '/resources/0-0-0/rules.md']
    setup_result = invocation.setup(threading.Event())
    assert setup_result is not None and setup_result.status == 'completed'
    assert (sandbox.cwd / 'working-directory').read_text().strip() == str(sandbox.cwd.resolve())
    assert (sandbox.home / 'tool').read_text().strip() == 'configured'
    diagnostic = invocation.dump_effective_config(threading.Event())
    assert diagnostic.status == 'available'
    if frozen:
        assert diagnostic.path is not None
        effective = read_json(run / diagnostic.path)
        assert effective['instructions'] == ['resources/0-0-0/rules.md']
        assert str(sandbox.runtime_files) not in json.dumps(effective)
    result, tracker = invocation.run(prompt, threading.Event())
    assert result.status == 'completed'
    assert tracker.root_session_id == 'fixture'
    assert (sandbox.cwd / 'result.txt').read_text() == 'edited\n'
    assert (sandbox.home / 'prompt').read_bytes() == prompt


@native
def test_native_launch_cancellation_stops_before_timeout(tmp_path: Path, sandbox: SeatbeltSandbox) -> None:
    sandbox.preflight()
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_process(
            sandbox.command(['/bin/sleep', '30']), env=sandbox.environment({}), cwd=sandbox.source,
            timeout=30, grace=0.1, cancel=cancel, log=tmp_path / 'cancel.log',
        )
        assert result.status == 'cancelled'
        assert time.monotonic() - started < 3
    finally:
        timer.join()


@native
def test_native_real_opencode_version_and_config(tmp_path: Path) -> None:
    """Optional pinned-runtime smoke uses no credentials and makes no model request."""
    executable = os.environ.get('BENCHMARK_TEST_OPENCODE')
    if executable is None:
        pytest.skip('Set BENCHMARK_TEST_OPENCODE for the pinned binary smoke')
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = SeatbeltSandbox(
        '/usr/bin/sandbox-exec', source, source, tmp_path / 'home', tmp_path / 'runtime', Path(executable)
    )
    sandbox.preflight()
    environment = sandbox.environment({}, config_content='{}')
    for arguments in (['--version'], ['debug', 'config']):
        result = subprocess.run(
            sandbox.command([sandbox.paths.binary, *arguments]),
            cwd=source, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        if arguments == ['--version']:
            assert result.stdout.strip() == '1.18.33'
        else:
            assert isinstance(json.loads(result.stdout), dict)


class DetachedDescendantSurvived(AssertionError):
    """Only the demonstrated lifecycle failure may be an expected failure."""


@native
@pytest.mark.xfail(
    strict=True, raises=DetachedDescendantSurvived,
    reason='Native execution needs a supervisor for detached descendants',
)
def test_native_detached_descendants_are_reaped(sandbox: SeatbeltSandbox, tmp_path: Path) -> None:
    """Track the lifecycle gate explicitly; always clean up the owned probe process."""
    source = sandbox.source / 'detach.c'
    binary = sandbox.source / 'detach'
    pid_file = sandbox.source / 'detached.pid'
    source.write_text('''#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    int ready[2];
    if (pipe(ready)) return 2;
    pid_t child = fork();
    if (child < 0) return 3;
    if (child > 0) {
        close(ready[1]);
        char c;
        return read(ready[0], &c, 1) == 1 ? 0 : 4;
    }
    close(ready[0]);
    if (setsid() < 0) _exit(5);
    FILE *file = fopen(argv[1], "w");
    if (!file) _exit(6);
    fprintf(file, "%d", getpid()); fclose(file);
    write(ready[1], "x", 1); close(ready[1]);
    sleep(20);
    return 0;
}
''')
    subprocess.run(['/usr/bin/cc', str(source), '-o', str(binary)], capture_output=True, check=True, timeout=30)
    sandbox.preflight()
    child = None
    try:
        result = run_process(
            sandbox.command([str(binary), str(pid_file)]), env=sandbox.environment({}), cwd=sandbox.source,
            timeout=5, grace=0.1, cancel=threading.Event(), log=tmp_path / 'detach.log',
        )
        assert result.status == 'completed'
        child = int(pid_file.read_text())
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        raise DetachedDescendantSurvived('Detached native descendant survived launch cleanup')
    finally:
        if child is None and pid_file.exists():
            child = int(pid_file.read_text())
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
