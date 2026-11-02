"""Native lifecycle regressions; no model access or host service persistence."""

import ctypes
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.execution.macos_processes import NativeProcesses
from benchmark.execution.macos_supervisor import run_macos_process


pytestmark = pytest.mark.skipif(platform.system() != 'Darwin', reason='macOS kernel process identities')


def options(root: Path, **overrides: object) -> dict:
    return dict(env={'PATH': '/usr/bin:/bin'}, cwd=root, timeout=10, grace=.1,
                cancel=threading.Event(), log=root / 'log', **overrides)


def test_pid_version_prevents_signalling_reused_identity() -> None:
    api = NativeProcesses()
    child = subprocess.Popen(['/bin/sleep', '20'])
    try:
        identity = api.identity(child.pid)
        assert identity is not None
        # Exercise the kernel check, not just the Python pre-check.
        token = (ctypes.c_uint32 * 8)()
        token[5], token[7] = child.pid, identity.version + 1
        assert api.library.proc_signal_with_audittoken(ctypes.byref(token), signal.SIGTERM) == 3
        assert child.poll() is None
        assert not api.send(replace(identity, version=identity.version + 1), signal.SIGTERM)
        assert api.send(identity, signal.SIGTERM)
        assert child.wait(timeout=5) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_caller_coalition_is_never_cleaned() -> None:
    api = NativeProcesses()
    own = api.identity(os.getpid())
    with pytest.raises(BenchmarkError, match='caller coalition'):
        api.cleanup(own.coalition)


def test_native_streams_stdin_and_redacts_environment(tmp_path: Path) -> None:
    lines, starts = [], []
    settings = options(tmp_path)
    settings['env']['TOKEN'] = 'ONLY_IN_MEMORY_SECRET'
    result = run_macos_process(
        ['/bin/sh', '-c', 'read value; printf "%s:%s\\n" "$value" "$TOKEN"; exit 7'],
        **settings, stdin=b'hello\n', secrets=['ONLY_IN_MEMORY_SECRET'],
        on_stdout=lines.append, on_start=lambda: starts.append(True),
    )
    assert result.status == 'error' and result.exit_code == 7
    assert lines == ['hello:<redacted>'] and starts == [True]
    assert 'ONLY_IN_MEMORY_SECRET' not in settings['log'].read_text()


@pytest.mark.parametrize('reason', ['timeout', 'cancelled', 'callback-error'])
def test_native_teardown_on_interruption(tmp_path: Path, reason: str) -> None:
    api = NativeProcesses()
    pid_file = tmp_path / 'child.pid'
    # fork plus setsid outlives the direct process and cannot be killed by killpg alone.
    program = (
        'import os,time; p=os.fork(); '
        f'os.setsid() if p==0 else None; open({str(pid_file)!r},"w").write(str(os.getpid())) if p==0 else None; '
        'time.sleep(20)'
    )
    unrelated = subprocess.Popen(['/bin/sleep', '20'])
    settings = options(tmp_path)
    timer = None

    def started() -> None:
        if reason == 'callback-error':
            # Let the detached child publish its PID before exercising exception cleanup.
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            raise ValueError('test callback failure')

    if reason == 'timeout':
        settings['timeout'] = 1
    if reason == 'cancelled':
        timer = threading.Timer(1, settings['cancel'].set)
        timer.start()
    try:
        if reason == 'callback-error':
            with pytest.raises(ValueError, match='test callback failure'):
                run_macos_process([sys.executable, '-I', '-c', program], **settings, on_start=started)
        else:
            result = run_macos_process([sys.executable, '-I', '-c', program], **settings, on_start=started)
            assert result.status == reason
        assert pid_file.exists()
        child = api.identity(int(pid_file.read_text()))
        assert child is None or child.status == 5
        assert unrelated.poll() is None
    finally:
        if timer is not None:
            timer.cancel()
        unrelated.kill()
        unrelated.wait()


def test_guardian_survives_coordinator_sigkill(tmp_path: Path) -> None:
    api = NativeProcesses()
    pid_file = tmp_path / 'child.pid'
    program = (
        'import os,time; p=os.fork(); '
        f'os.setsid() if p==0 else None; open({str(pid_file)!r},"w").write(str(os.getpid())) if p==0 else None; '
        'time.sleep(20)'
    )
    driver = '''import sys,threading
from pathlib import Path
import benchmark.execution.macos_supervisor as module
original = module._save
def record(root, name, value):
 original(root, name, value)
 if name == 'caller.json':
  (Path(sys.argv[1])/'control.txt').write_text(str(root))
module._save = record
module.run_macos_process([sys.executable,'-I','-c',sys.argv[2]], env={},cwd=Path(sys.argv[1]),
 timeout=20,grace=.1,cancel=threading.Event(),log=Path(sys.argv[1])/'log')
'''
    process = subprocess.Popen([sys.executable, '-I', '-c', driver, str(tmp_path), program])
    identity = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.02)
        assert pid_file.exists()
        identity = api.identity(int(pid_file.read_text()))
        assert identity is not None
        control = Path((tmp_path / 'control.txt').read_text())
        target = json.loads((control / 'job.json').read_text())['target']
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            child = api.identity(identity.pid)
            if (child is None or child.status == 5) and not control.exists():
                state = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True)
                if state.returncode != 0:
                    break
            time.sleep(.02)
        else:
            pytest.fail('Detached descendant survived coordinator SIGKILL')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if identity is not None:
            api.cleanup(identity.coalition)


def test_preset_cancel_does_not_issue_payload(tmp_path: Path) -> None:
    settings = options(tmp_path)
    settings['cancel'].set()
    starts = []
    result = run_macos_process(['/usr/bin/touch', str(tmp_path / 'issued')], **settings,
                               on_start=lambda: starts.append(True))
    assert result.status == 'cancelled' and result.exit_code is None
    assert not starts and not (tmp_path / 'issued').exists()


@pytest.mark.parametrize('target', ['client', 'guardian'])
def test_cleanup_after_supervision_process_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    import benchmark.execution.macos_supervisor as module

    api = NativeProcesses()
    real_run = module.run_process
    root = None

    def capture(argv: list[str], **settings: object):
        nonlocal root
        root = Path(argv[5])
        return real_run(argv, **settings)

    monkeypatch.setattr(module, 'run_process', capture)
    pid_file = tmp_path / 'payload.pid'
    identity = None

    def crash() -> None:
        nonlocal identity
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        identity = api.identity(int(pid_file.read_text()))
        assert identity is not None
        if target == 'guardian':
            guardian = module.ProcessIdentity(**module._load(root, 'guardian.json'))
            api.send(guardian, signal.SIGKILL)
        else:
            # The client PID is deliberately recorded by the trusted test wrapper.
            client = module.ProcessIdentity(**module._load(root, 'client.json'))
            api.send(client, signal.SIGKILL)

    program = f'import os,time;open({str(pid_file)!r},"w").write(str(os.getpid()));time.sleep(20)'
    try:
        if target == 'guardian':
            with pytest.raises(BenchmarkError, match='native process guardian'):
                run_macos_process([sys.executable, '-I', '-c', program], **options(tmp_path), on_start=crash)
        else:
            result = run_macos_process([sys.executable, '-I', '-c', program], **options(tmp_path), on_start=crash)
            assert result.status == 'error'
        current = api.identity(identity.pid)
        assert current is None or current.status == 5
        assert not root.exists()
    finally:
        if identity is not None:
            api.cleanup(identity.coalition)


def test_concurrent_invocations_have_independent_coalitions(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def launch(index: int):
        root = tmp_path / str(index)
        root.mkdir()
        command = ['/bin/sh', '-c', f'sleep {index / 3}; printf {index}']
        result = run_macos_process(command, **options(root), on_start=lambda: barrier.wait(timeout=5))
        return result, (root / 'log').read_text()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(launch, [0, 1]))
    assert [result.status for result, _ in results] == ['completed', 'completed']
    assert [output for _, output in results] == ['0', '1']


def test_launchd_files_do_not_contain_agent_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import benchmark.execution.macos_supervisor as module

    real_run = module.run_process
    root = None

    def capture(argv: list[str], **settings: object):
        nonlocal root
        root = Path(argv[5])
        return real_run(argv, **settings)

    monkeypatch.setattr(module, 'run_process', capture)
    settings = options(tmp_path)
    settings['env']['TOKEN'] = 'SECRET_ONLY_THROUGH_SOCKET'

    def inspect() -> None:
        assert root.stat().st_mode & 0o777 == 0o700
        assert all(b'SECRET_ONLY_THROUGH_SOCKET' not in path.read_bytes()
                   for path in root.iterdir() if path.is_file())

    result = run_macos_process(['/bin/sleep', '.1'], **settings, on_start=inspect)
    assert result.status == 'completed'
    assert not root.exists()
