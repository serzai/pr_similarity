"""Ephemeral launchd guardian for Seatbelt processes, including detached children.

The private socket carries environment and stdio descriptors in memory. Neither
provider credentials nor the agent command are written to launchd property lists.
The guardian lives outside the agent policy and survives the launch client's death.
"""

import array
import json
import os
import plistlib
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from benchmark.errors import BenchmarkError
from benchmark.execution.macos_processes import NativeProcesses, ProcessIdentity
from benchmark.execution.process import ProcessResult, run_process


def _save(root: Path, name: str, value: Any) -> None:
    temporary = root / (name + '.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(root / name)


def _load(root: Path, name: str) -> Any:
    return json.loads((root / name).read_text())


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['/bin/launchctl', *arguments], env={'PATH': '/usr/bin:/bin'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )


def _receive(connection: socket.socket, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        part = connection.recv(size - len(content))
        if not part:
            raise EOFError('Launch client disconnected')
        content.extend(part)
    return bytes(content)


def _guardian(root: Path) -> int:
    api = NativeProcesses()
    own = api.identity(os.getpid())
    caller = ProcessIdentity(**_load(root, 'caller.json'))
    if own is None or own.coalition <= 0 or own.coalition == caller.coalition:
        _save(root, 'result.json', {'error': 'native_cleanup_unavailable'})
        if (root / 'job.json').exists():
            _launchctl('bootout', _load(root, 'job.json')['target'])
        return 1
    # Durable identity is published before any agent process can exist.
    _save(root, 'guardian.json', asdict(own))
    descriptors = array.array('i')
    child = None
    result: dict[str, Any] = {'error': 'native_supervisor_failed'}
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect(str(root / 'socket'))
            _, ancillary, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(3 * descriptors.itemsize))
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    descriptors.frombytes(data[:len(data) - len(data) % descriptors.itemsize])
            if flags & socket.MSG_CTRUNC or len(descriptors) != 3:
                raise ValueError('Missing launch descriptors')
            length = int.from_bytes(_receive(connection, 8), 'big')
            if length > 16 * 1024 * 1024:
                raise ValueError('Launch request exceeds limit')
            request = json.loads(_receive(connection, length))
            # EOF before issuance means cancellation; do not start an orphan agent.
            connection.setblocking(False)
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_READ)
                if selector.select(0):
                    raise EOFError('Launch cancelled')
                child = subprocess.Popen(
                    request['argv'], cwd=request['cwd'], env=request['env'],
                    stdin=descriptors[0], stdout=descriptors[1], stderr=descriptors[2],
                    close_fds=True, start_new_session=True, umask=0o077,
                )
                _save(root, 'started.json', {'started': True})
                while child.poll() is None:
                    if selector.select(.05):
                        # Any input, including EOF, requests complete teardown.
                        break
                result = {'exit_code': child.poll()}
    except (OSError, ValueError, EOFError, subprocess.SubprocessError):
        pass
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        try:
            api.cleanup(own.coalition, guardian=own)
            if child is not None:
                code = child.wait(timeout=5)
                if 'error' not in result and result['exit_code'] is None:
                    result['exit_code'] = code
        except (BenchmarkError, OSError, subprocess.SubprocessError):
            result = {'error': 'native_cleanup_failed'}
        _save(root, 'result.json', result)
        # The client may have died with its coordinator. Remove the ephemeral job too.
        if (root / 'job.json').exists():
            target = _load(root, 'job.json')['target']
            current = api.identity(caller.pid)
            if current is None or (current.unique, current.version) != (caller.unique, caller.version):
                shutil.rmtree(root)
            _launchctl('bootout', target)
    return 0 if 'error' not in result else 1


def _client(root: Path, parent: int, argv: list[str]) -> int:
    stopping = False

    def stop(_number: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    label = 'com.prsim.attempt.' + root.name.removeprefix('prsim-')
    try:
        client = NativeProcesses().identity(os.getpid())
        if client is None:
            raise RuntimeError('Cannot identify native launch client')
        _save(root, 'client.json', asdict(client))
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(root / 'socket'))
            listener.listen(1)
            listener.settimeout(.05)
            plist = root / 'guardian.plist'
            plist.write_bytes(plistlib.dumps({
                'Label': label,
                'ProgramArguments': [
                    sys.executable, '-I', '-m', 'benchmark.execution.macos_supervisor', '--guardian', str(root),
                ],
                'RunAtLoad': True, 'KeepAlive': False,
                'EnvironmentVariables': {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'},
            }))
            # GUI domains exist for desktop sessions, user domains for headless hosts.
            domain = None
            for candidate in (f'gui/{os.getuid()}', f'user/{os.getuid()}'):
                # Record the candidate first so the coordinator can boot it out if we die mid-bootstrap.
                _save(root, 'job.json', {'target': candidate + '/' + label})
                if _launchctl('bootstrap', candidate, str(plist)).returncode == 0:
                    domain = candidate
                    break
            if domain is None:
                raise RuntimeError('launchd domain unavailable')
            deadline = time.monotonic() + 10
            while not stopping and os.getppid() == parent and time.monotonic() < deadline:
                try:
                    connection, _ = listener.accept()
                    break
                except TimeoutError:
                    if (root / 'result.json').exists():
                        return 1
            else:
                return 1
            with connection:
                payload = json.dumps({'argv': argv, 'env': dict(os.environ), 'cwd': os.getcwd()}).encode()
                descriptors = array.array('i', [0, 1, 2])
                connection.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
                connection.sendall(len(payload).to_bytes(8, 'big') + payload)
                api = NativeProcesses()
                guardian = ProcessIdentity(**_load(root, 'guardian.json'))
                while not (root / 'result.json').exists():
                    if stopping or os.getppid() != parent:
                        connection.shutdown(socket.SHUT_WR)
                        if os.getppid() != parent:
                            return 1
                        break
                    current = api.identity(guardian.pid)
                    if current is None or current.unique != guardian.unique or current.status == 5:
                        raise RuntimeError('Native guardian exited before cleanup receipt')
                    time.sleep(.02)
                # Stay alive until cleanup finishes; the outer timeout may still SIGKILL us.
                deadline = time.monotonic() + 10
                while not (root / 'result.json').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
            return 0 if (root / 'result.json').exists() else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Deliberately do not print request, environment or native launchd diagnostics.
        _save(root, 'client-error.json', {'error': 'native_supervisor_unavailable'})
        return 1


def run_macos_process(argv: list[str], **options: Any) -> ProcessResult:
    """Reuse streaming/redaction while placing the payload in a private coalition."""
    started_at = time.monotonic()
    api = NativeProcesses()
    own = api.identity(os.getpid())
    if own is None:
        raise BenchmarkError('native_cleanup_unavailable', 'Cannot identify the native launch owner.')
    cancel: threading.Event = options['cancel']
    callback = options.pop('on_start', None)
    issued = False
    with tempfile.TemporaryDirectory(prefix='prsim-', dir='/private/tmp') as temporary:
        root = Path(temporary)
        _save(root, 'caller.json', asdict(own))

        def actual_start() -> None:
            nonlocal issued
            deadline = started_at + min(options['timeout'], 12)
            while not (root / 'started.json').exists():
                if cancel.is_set() or time.monotonic() >= deadline or any(
                    (root / name).exists() for name in ('result.json', 'client-error.json')
                ):
                    if not cancel.is_set() and time.monotonic() >= deadline and options['timeout'] > 12:
                        raise BenchmarkError('native_supervisor_unavailable', 'Native guardian startup timed out.')
                    return
                time.sleep(.01)
            issued = True
            if callback is not None:
                callback()

        try:
            result = run_process(
                [sys.executable, '-I', '-m', 'benchmark.execution.macos_supervisor',
                 '--client', str(root), str(os.getpid()), *argv],
                on_start=actual_start, **options,
            )
        finally:
            # A killed client closes its socket. Wait for the independent guardian.
            deadline = time.monotonic() + 12
            while (root / 'job.json').exists() and not (root / 'result.json').exists():
                if time.monotonic() >= deadline or (root / 'client-error.json').exists():
                    break
                if (root / 'guardian.json').exists():
                    identity = ProcessIdentity(**_load(root, 'guardian.json'))
                    current = api.identity(identity.pid)
                    if current is None or current.unique != identity.unique or current.status == 5:
                        break
                time.sleep(.02)
            if (root / 'guardian.json').exists():
                guardian = ProcessIdentity(**_load(root, 'guardian.json'))
                if guardian.coalition == own.coalition:
                    raise BenchmarkError('unsafe_process_cleanup', 'Native guardian shares the caller coalition.')
                # Verify cleanup independently, also covering guardian failure.
                api.cleanup(guardian.coalition)
            if (root / 'job.json').exists():
                _launchctl('bootout', _load(root, 'job.json')['target'])
        if not issued and (root / 'started.json').exists():
            if callback is not None:
                callback()
        if (root / 'result.json').exists():
            receipt = _load(root, 'result.json')
            if 'error' in receipt:
                raise BenchmarkError(receipt['error'], 'Native process supervision or cleanup failed.')
            code = receipt['exit_code']
            status = result.status if result.status in ('timeout', 'cancelled') else (
                'completed' if code == 0 else 'error'
            )
            return ProcessResult(status, code, time.monotonic() - started_at, result.warnings)
        if result.status == 'cancelled' and result.exit_code is None:
            return result
        raise BenchmarkError('native_supervisor_unavailable', 'Cannot start the native process guardian.')


if __name__ == '__main__':
    if sys.argv[1] == '--guardian':
        sys.exit(_guardian(Path(sys.argv[2])))
    if sys.argv[1] == '--client':
        sys.exit(_client(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))
    sys.exit(2)
