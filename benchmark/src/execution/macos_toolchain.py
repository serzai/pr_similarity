"""Prepare an explicit content-addressed Apple C/C++ toolchain for native setup."""

import hashlib
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


def _tool(argv: list[str], root: Path, cancel: threading.Event | None) -> list[str]:
    lines: list[str] = []
    result = run_process(
        argv, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, cwd=root,
        timeout=30, grace=.2, cancel=cancel or threading.Event(),
        log=root / 'preparation.log', on_stdout=lines.append,
    )
    if result.status == 'cancelled':
        check_cancelled(cancel)
    if result.status != 'completed':
        raise BenchmarkError('native_toolchain_unavailable', 'Apple C/C++ toolchain preparation failed.')
    return lines


def toolchain_digest(root: Path, cancel: threading.Event | None = None) -> str:
    """Measure files, directory names and internal links without following SDK cycles."""
    root = root.resolve()
    identity = hashlib.sha256()
    for base, directories, files in os.walk(root, followlinks=False):
        directories.sort(); files.sort()
        for name in sorted(directories + files):
            check_cancelled(cancel)
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                anchored = Path(os.path.normpath(path.parent / target))
                if Path(target).is_absolute() or not anchored.is_relative_to(root):
                    raise BenchmarkError('unsafe_toolchain', 'Toolchain link escapes its private snapshot.')
                record = f'link\0{relative}\0{target}\n'
            elif path.is_dir():
                record = f'dir\0{relative}\n'
            elif path.is_file():
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                record = f'file\0{relative}\0{digest}\0{path.stat().st_size}\n'
            else:
                raise BenchmarkError('unsafe_toolchain', 'Toolchain contains a special file.')
            identity.update(record.encode())
    return identity.hexdigest()


def prepare_macos_toolchain(cache: Path, *, cancel: threading.Event | None = None) -> Path:
    """Copy selected trusted compiler/linker code, resources and SDK, never host HOME."""
    directory = cache.expanduser().resolve() / 'macos-toolchain'
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / '.lock', cancel=cancel, wait_forever=True):
        with TemporaryDirectory(prefix='prepare-', dir=directory) as temporary:
            staging = Path(temporary)
            clang = Path(_tool(['/usr/bin/xcrun', '--find', 'clang'], staging, cancel)[0]).resolve()
            sdk = Path(_tool(['/usr/bin/xcrun', '--show-sdk-path'], staging, cancel)[0]).resolve()
            if not sdk.is_dir() or not (sdk / 'usr/include').is_dir():
                raise BenchmarkError('native_toolchain_unavailable', 'Selected Apple SDK has no C headers.')
            bundle = staging / 'bundle'
            (bundle / 'bin').mkdir(parents=True)
            (bundle / 'lib').mkdir()
            pending: list[Path] = []
            for name in ('clang', 'ld', 'ar', 'ranlib', 'nm', 'strip'):
                program = Path(_tool(['/usr/bin/xcrun', '--find', name], staging, cancel)[0]).resolve()
                shutil.copyfile(program, bundle / 'bin' / name)
                (bundle / 'bin' / name).chmod(0o755)
                pending.append(program)
            for name in ('clang++', 'cc', 'c++'):
                (bundle / 'bin' / name).symlink_to('clang')
            visited: set[Path] = set()
            while pending:
                program = pending.pop()
                if program in visited:
                    continue
                visited.add(program)
                for library in dependencies(_tool(['/usr/bin/otool', '-L', str(program)], staging, cancel)):
                    if library.startswith(('/usr/lib/', '/System/Library/')):
                        continue
                    if not library.startswith('@rpath/') or '/' in library.removeprefix('@rpath/'):
                        raise BenchmarkError('native_toolchain_unavailable', 'Unsupported Apple toolchain library path.')
                    source = (clang.parent.parent / 'lib' / library.removeprefix('@rpath/')).resolve()
                    if not source.is_relative_to(clang.parent.parent / 'lib') or not source.is_file():
                        raise BenchmarkError('native_toolchain_unavailable', 'Apple toolchain library is unavailable.')
                    target = bundle / 'lib' / library.removeprefix('@rpath/')
                    if not target.exists():
                        shutil.copyfile(source, target)
                        target.chmod(0o755)
                        pending.append(source)
            shutil.copytree(clang.parent.parent / 'lib/clang', bundle / 'lib/clang', symlinks=True)
            shutil.copytree(sdk, bundle / 'sdk', symlinks=True)
            identity = toolchain_digest(bundle, cancel)
            target = directory / identity
            if target.exists():
                if target.is_symlink() or toolchain_digest(target, cancel) != identity:
                    raise BenchmarkError('native_toolchain_changed', 'Cached Apple toolchain failed verification.')
            else:
                os.rename(bundle, target)
            return target
