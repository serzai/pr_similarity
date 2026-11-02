"""Native policy enforcement checks using harmless files, with no model access."""

import platform
import socket
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from benchmark.errors import BenchmarkError
from benchmark.execution.seatbelt import SeatbeltPolicy


@pytest.fixture
def policy(tmp_path: Path) -> SeatbeltPolicy:
    """Use paths with spaces, Unicode and profile syntax to test parameter handling."""
    root = tmp_path / 'attempt Мак " (deny default)'
    root.mkdir()
    for name in ('source', 'home', 'temporary', 'runtime'):
        (root / name).mkdir()
    (root / 'source/.git').mkdir()
    (root / 'source/.git/HEAD').write_text('ref: refs/heads/before\n')
    return SeatbeltPolicy(root / 'source', root / 'home', root / 'temporary', root / 'runtime', Path('/bin/sh'))


def invoke(policy: SeatbeltPolicy, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a native shell with an explicit minimal environment and bounded lifetime."""
    return subprocess.run(
        policy.command(['/bin/sh', '-c', script, 'probe', *arguments]),
        cwd=policy.source,
        env={'HOME': str(policy.home), 'TMPDIR': str(policy.temporary), 'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_profile_paths_are_parameters(policy: SeatbeltPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quotes and Scheme syntax in paths never become executable policy text."""
    monkeypatch.setattr('benchmark.execution.seatbelt.platform.system', lambda: 'Darwin')
    command = policy.command(['/usr/bin/true'])
    assert str(policy.source.resolve()) not in policy.profile()
    assert 'SOURCE=' + str(policy.source.resolve()) in command
    assert '(deny default)' in policy.profile()
    assert '(allow default)' not in policy.profile()
    assert command[-1] == '/usr/bin/true'


@pytest.mark.parametrize('root', ['/', 'host-home', 'overlap'])
def test_rejects_unsafe_layout(policy: SeatbeltPolicy, root: str) -> None:
    """An attempt cannot turn a broad host root into private writable state."""
    home = Path('/') if root == '/' else Path.home() if root == 'host-home' else policy.source / 'home'
    with pytest.raises(BenchmarkError) as error:
        SeatbeltPolicy(policy.source, home, policy.temporary, policy.runtime, policy.binary)
    assert error.value.code == 'unsafe_sandbox'


native = pytest.mark.skipif(platform.system() != 'Darwin', reason='macOS Seatbelt integration')


@native
def test_native_process_and_private_writes(policy: SeatbeltPolicy) -> None:
    """Positive controls prove a denied operation is not merely a broken sandbox."""
    result = invoke(policy, 'echo source > local; echo home > "$HOME/state"; echo temp > "$TMPDIR/file"; cat local')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'source'
    assert (policy.home / 'state').read_text() == 'home\n'
    assert (policy.temporary / 'file').read_text() == 'temp\n'


@native
@pytest.mark.parametrize('name', ['host-home', 'oauth', 'git-cache', 'after-revision', 'sibling-attempt'])
def test_native_forbidden_file_read_and_write(policy: SeatbeltPolicy, tmp_path: Path, name: str) -> None:
    """Independent forbidden roots remain inaccessible to source-owned subprocesses."""
    forbidden = tmp_path / name
    forbidden.mkdir()
    secret = forbidden / 'secret'
    secret.write_text('private fixture')
    for script in ('cat "$1"', 'echo changed > "$1"'):
        result = invoke(policy, script, str(secret))
        assert result.returncode != 0
        assert 'private fixture' not in result.stdout
    assert secret.read_text() == 'private fixture'
    assert invoke(policy, 'stat "$1"', str(secret)).returncode != 0


@native
def test_native_symlink_cannot_escape_source(policy: SeatbeltPolicy, tmp_path: Path) -> None:
    """Kernel policy follows symlinks rather than trusting their allowed lexical path."""
    secret = tmp_path / 'secret'
    secret.write_text('private fixture')
    (policy.source / 'escape').symlink_to(secret)
    assert invoke(policy, 'cat escape').returncode != 0
    assert invoke(policy, 'echo changed > escape').returncode != 0
    assert secret.read_text() == 'private fixture'


@native
def test_native_data_volume_alias_is_not_a_system_library(policy: SeatbeltPolicy, tmp_path: Path) -> None:
    """A /System allowlist must not expose the writable Data volume and host files."""
    secret = tmp_path / 'private-data'
    secret.write_text('private fixture')
    alias = Path('/System/Volumes/Data') / secret.resolve().relative_to('/')
    assert alias.is_file()
    result = invoke(policy, 'cat "$1"', str(alias))
    assert result.returncode != 0
    assert 'private fixture' not in result.stdout


@native
def test_native_var_alias_uses_the_same_private_directory() -> None:
    """Lexical /var paths remain usable when the kernel resolves them through /private."""
    with TemporaryDirectory(prefix='prsim-alias-', dir='/var/tmp') as directory:
        root = Path(directory)
        for name in ('source', 'home', 'temporary', 'runtime'):
            (root / name).mkdir()
        policy = SeatbeltPolicy(root / 'source', root / 'home', root / 'temporary', root / 'runtime', Path('/bin/sh'))
        (policy.source / 'content').write_text('private content')
        assert str(policy.source).startswith('/var/')
        assert str(policy.source.resolve()).startswith('/private/var/')
        result = invoke(policy, 'cat "$1"', str(policy.source / 'content'))
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'private content'


@native
def test_native_runtime_and_git_are_readonly(policy: SeatbeltPolicy) -> None:
    """Setup/agent may inspect inputs and baseline metadata but cannot rewrite them."""
    resource = policy.runtime / 'rules'
    resource.write_text('read only')
    assert invoke(policy, 'cat "$1"', str(resource)).returncode == 0
    assert invoke(policy, 'echo changed > "$1"', str(resource)).returncode != 0
    assert invoke(policy, 'cat .git/HEAD').returncode == 0
    assert invoke(policy, 'echo changed > .git/HEAD').returncode != 0
    assert invoke(policy, 'mv .git saved-git').returncode != 0
    assert resource.read_text() == 'read only'


@native
def test_native_parent_directory_cannot_be_enumerated(policy: SeatbeltPolicy) -> None:
    """Metadata lookup does not grant directory listing outside dedicated roots."""
    result = invoke(policy, 'ls "$1"', str(policy.source.parent))
    assert result.returncode != 0


@native
def test_native_host_process_cannot_be_signaled(policy: SeatbeltPolicy) -> None:
    """A child can manage its own sandbox processes, not the benchmark host."""
    host = subprocess.Popen(['/bin/sleep', '20'], env={'PRSIM_FIXTURE_SECRET': 'private-process-marker'})
    try:
        result = invoke(policy, 'kill -TERM "$1"', str(host.pid))
        assert result.returncode != 0
        assert host.poll() is None
        inspected = invoke(policy, 'ps -E -p "$1" -o command=', str(host.pid))
        assert 'sleep 20' not in inspected.stdout
        assert 'private-process-marker' not in inspected.stdout
        own = invoke(policy, 'sleep 20 & child=$!; kill -TERM "$child"; wait "$child"; exit 0')
        assert own.returncode == 0, own.stderr
    finally:
        host.terminate()
        host.wait(timeout=5)


@native
def test_native_network_is_shared_without_host_unix_sockets(policy: SeatbeltPolicy, tmp_path: Path) -> None:
    """IP access is intentional; arbitrary host Unix sockets remain unavailable."""
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        server.settimeout(5)
        port = server.getsockname()[1]
        received = []

        def accept() -> None:
            with server.accept()[0] as connection:
                received.append(connection.recv(8))

        thread = threading.Thread(target=accept)
        thread.start()
        try:
            result = invoke(policy, 'printf hello | nc -w 1 127.0.0.1 "$1"', str(port))
            assert result.returncode == 0, result.stderr
        finally:
            thread.join(timeout=6)
        assert received == [b'hello']
    with TemporaryDirectory(prefix='prsim-sock-', dir='/tmp') as directory, socket.socket(socket.AF_UNIX) as server:
        # AF_UNIX paths on macOS are limited to 104 bytes.
        path = Path(directory) / 'host.sock'
        server.bind(str(path))
        server.listen()
        result = invoke(policy, 'printf hello | nc -U -w 1 "$1"', str(path))
        assert result.returncode != 0
