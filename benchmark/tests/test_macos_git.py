"""Private Git bytes and real Git builtin execution inside the native policy."""

import hashlib
import json
import platform
import subprocess
import threading
from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.execution.macos_git import _PROGRAMS, install_macos_git, prepare_macos_git
from benchmark.execution.seatbelt import SeatbeltSandbox
from benchmark.vcs.git import git_environment


def test_git_install_rejects_changed_snapshot(tmp_path: Path) -> None:
    files = {name: b'git-fixture' for name in _PROGRAMS}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    snapshot = tmp_path / digest
    snapshot.mkdir()
    for name, value in files.items():
        (snapshot / name).write_bytes(value)
    (snapshot / 'git').write_bytes(b'changed')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    with pytest.raises(BenchmarkError, match='changed before the attempt'):
        install_macos_git(snapshot, runtime)


@pytest.mark.skipif(platform.system() != 'Darwin', reason='macOS Apple Git integration')
def test_git_snapshot_and_builtins_are_usable_but_readonly(tmp_path: Path) -> None:
    root = prepare_macos_git(tmp_path / 'cache')
    assert prepare_macos_git(tmp_path / 'cache') == root
    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['/usr/bin/git', 'init', '-q', str(source)], env=git_environment(), check=True)
    (source / 'before.txt').write_text('before')
    sandbox = SeatbeltSandbox('/usr/bin/sandbox-exec', source, source, tmp_path / 'home',
                              tmp_path / 'runtime', Path('/bin/sh'), git=root)
    result = sandbox.run_process(
        ['/bin/sh', '-c', 'git config --global fixture.private yes && git status --porcelain '
         '&& ! printf changed > "$GIT_EXEC_PATH/git"'],
        env=sandbox.environment({}), cwd=source, timeout=10, grace=.1,
        cancel=threading.Event(), log=tmp_path / 'git.log',
    )
    assert result.status == 'completed'
    assert '[fixture]' in (sandbox.home / '.gitconfig').read_text()
    assert '?? before.txt' in (tmp_path / 'git.log').read_text()
    assert str(tmp_path) not in (tmp_path / 'git.log').read_text()
    original = (root / 'git').read_bytes()
    (root / 'git').write_bytes(b'changed')
    try:
        with pytest.raises(BenchmarkError, match='content verification'):
            prepare_macos_git(tmp_path / 'cache')
    finally:
        (root / 'git').write_bytes(original)
