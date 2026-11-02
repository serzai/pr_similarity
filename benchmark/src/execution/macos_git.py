"""Freeze actual Apple Git executables without exposing a developer-tools directory."""

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.errors import BenchmarkError
from benchmark.execution.cancellation import check_cancelled
from benchmark.execution.macos_python import dependencies
from benchmark.execution.process import run_process
from benchmark.persistence.storage import file_lock


# Git's builtins use the main executable. HTTPS transports require its libcurl helper.
_PROGRAMS = ('git', 'git-remote-http', 'git-remote-https', 'git-upload-pack', 'git-receive-pack', 'git-upload-archive')


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _tool(argv: list[str], root: Path, cancel: threading.Event | None) -> list[str]:
    lines = []
    result = run_process(
        argv, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null'}, cwd=root, timeout=30, grace=.2,
        cancel=cancel or threading.Event(), log=root / 'git-preparation.log', on_stdout=lines.append,
    )
    if result.status == 'cancelled':
        check_cancelled(cancel)
    if result.status != 'completed':
        raise BenchmarkError('native_git_unavailable', 'Native Git preparation requires Apple Command Line Tools.')
    return lines


def prepare_macos_git(cache: Path, *, cancel: threading.Event | None = None) -> Path:
    """Select Git via xcrun on the host, copy only executables and verify cached bytes."""
    directory = cache.expanduser().resolve() / 'macos-git'
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='git-', dir=directory) as temporary:
        staging = Path(temporary)
        binary = Path(_tool(['/usr/bin/xcrun', '--find', 'git'], staging, cancel)[0]).resolve()
        core = Path(_tool([str(binary), '--exec-path'], staging, cancel)[0]).resolve()
        selected = {name: binary if name == 'git' else (core / name).resolve() for name in _PROGRAMS}
        hashes = {}
        for name, source in selected.items():
            check_cancelled(cancel)
            if not source.is_file() or (
                name != 'git' and source != binary and not source.is_relative_to(core)
            ):
                raise BenchmarkError('native_git_unavailable', 'Apple Git helper is missing or outside its tool directory.')
            libraries = dependencies(_tool(['/usr/bin/otool', '-L', str(source)], staging, cancel))
            if any(not library.startswith(('/usr/lib/', '/System/Library/')) for library in libraries):
                raise BenchmarkError('native_git_unavailable', 'Git needs an unsupported non-system native library.')
            target = staging / name
            shutil.copyfile(source, target)
            target.chmod(0o755)
            hashes[name] = _digest(target)
            if hashes[name] != _digest(source):
                raise BenchmarkError('native_git_changed', 'Git changed while preparing its snapshot.')
        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        target = directory / identity
        with file_lock(directory / '.lock', cancel=cancel, wait_forever=True):
            if target.exists():
                if target.is_symlink() or {path.name for path in target.iterdir()} != set(hashes) or any(
                    (target / name).is_symlink() or not (target / name).is_file() or _digest(target / name) != digest
                    for name, digest in hashes.items()
                ):
                    raise BenchmarkError('native_git_changed', 'Cached native Git failed content verification.')
            else:
                # Logs may contain host installation paths; they are not runtime resources.
                (staging / 'git-preparation.log').unlink(missing_ok=True)
                os.rename(staging, target)
        return target


def install_macos_git(snapshot: Path, runtime: Path) -> None:
    """Copy the frozen programs into the existing readonly attempt runtime."""
    target = runtime / 'git'
    target.mkdir(mode=0o700)
    hashes = {}
    for name in _PROGRAMS:
        source = snapshot / name
        if source.is_symlink() or not source.is_file():
            raise BenchmarkError('native_git_changed', 'Native Git snapshot contains an invalid executable.')
        shutil.copyfile(source, target / name)
        (target / name).chmod(0o755)
        hashes[name] = _digest(target / name)
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if identity != snapshot.name:
        raise BenchmarkError('native_git_changed', 'Native Git snapshot changed before the attempt.')
