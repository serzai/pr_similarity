"""Relocate a private macOS Python copy without granting access to host packages."""

import json
import os
import re
import shutil
import sysconfig
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.errors import BenchmarkError
from benchmark.execution.cancellation import check_cancelled
from benchmark.execution.process import run_process
from benchmark.execution.python_runtime import (
    PythonRuntime, _digest, interpreter_files, publish_python, python_receipt,
)

PREPARATION_VERSION = 'macho-relocation-v1'


def dependencies(output: list[str]) -> list[str]:
    """Read dependency load commands once across all slices of a universal binary."""
    return sorted({match[1] for line in output if (match := re.match(r'\s+(.+?) \(compatibility version ', line))})


def _system_library(name: str) -> bool:
    return name.startswith(('/usr/lib/', '/System/Library/'))


def resolve_library(name: str, binary: Path, executable: Path, rpaths: list[str]) -> Path:
    """Resolve Mach-O tokens before copying; unresolved dependencies are never inherited."""
    def expand(value: str) -> Path:
        if value.startswith('@loader_path/'):
            return binary.parent / value.removeprefix('@loader_path/')
        if value.startswith('@executable_path/'):
            return executable.parent / value.removeprefix('@executable_path/')
        if value.startswith('@') or not value.startswith('/'):
            raise BenchmarkError('python_layout_unsupported', 'Python uses an unsupported Mach-O library path.')
        return Path(value)

    candidates = (
        [expand(path) / name.removeprefix('@rpath/') for path in rpaths]
        if name.startswith('@rpath/') else [expand(name)]
    )
    existing = {path.resolve() for path in candidates if path.is_file()}
    if len(existing) != 1:
        raise BenchmarkError('python_library_missing', 'A Python Mach-O dependency is missing or ambiguous.')
    return existing.pop()


def _tool(argv: list[str], root: Path, cancel: threading.Event | None) -> list[str]:
    output = []
    result = run_process(
        argv, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, cwd=root,
        timeout=30, grace=0.2, cancel=cancel or threading.Event(),
        log=root / 'preparation.log', on_stdout=output.append,
    )
    if result.status == 'cancelled':
        check_cancelled(cancel)
    if result.status != 'completed':
        raise BenchmarkError(
            'python_layout_unsupported', 'Cannot relocate macOS Python; check Command Line Tools and code signing.'
        )
    return output


def _rpaths(binary: Path, executable: Path, root: Path, cancel: threading.Event | None) -> list[str]:
    output = _tool(['/usr/bin/otool', '-l', str(binary)], root, cancel)
    paths = []
    selected = False
    for line in output:
        if line.strip().startswith('cmd '):
            selected = line.strip() == 'cmd LC_RPATH'
        elif selected and (match := re.match(r'\s+path (.+) \(offset \d+\)', line)):
            path = match[1]
            if path == '@loader_path':
                path = str(binary.parent)
            elif path.startswith('@loader_path/'):
                path = str(binary.parent / path.removeprefix('@loader_path/'))
            elif path == '@executable_path':
                path = str(executable.parent)
            elif path.startswith('@executable_path/'):
                path = str(executable.parent / path.removeprefix('@executable_path/'))
            paths.append(path)
    return sorted(set(paths))


def prepare_macos_python(cache: Path, *, cancel: threading.Event | None = None) -> PythonRuntime:
    """Copy, redirect and ad-hoc sign private Mach-O files, then freeze their bytes."""
    for name in ('otool', 'install_name_tool', 'codesign'):
        if not Path('/usr/bin', name).is_file():
            raise BenchmarkError('python_layout_unsupported', 'macOS Python preparation requires Command Line Tools.')
    files = interpreter_files()
    executable = files['bin/python3.12']
    framework = sysconfig.get_config_var('PYTHONFRAMEWORK')
    if framework:
        # python.org's bin/python is a launcher that re-execs this framework member.
        member = executable.parent.parent / f'Resources/{framework}.app/Contents/MacOS/{framework}'
        if member.is_file():
            executable = member.resolve()
            files['bin/python3.12'] = executable
    initial = {path: name for name, path in files.items() if name.startswith('bin/') or path.suffix == '.so'}
    queue = list(initial)
    owners = dict(initial)
    links: dict[Path, dict[str, Path]] = {}
    cache = cache.expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='macos-python-', dir=cache) as temporary:
        root = Path(temporary)
        executable_rpaths = _rpaths(executable, executable, root, cancel)
        while queue:
            binary = queue.pop()
            check_cancelled(cancel)
            names = dependencies(_tool(['/usr/bin/otool', '-L', str(binary)], root, cancel))
            rpaths = _rpaths(binary, executable, root, cancel) + executable_rpaths
            links[binary] = {}
            for name in names:
                if _system_library(name):
                    continue
                library = resolve_library(name, binary, executable, rpaths)
                if library == binary:  # A dylib's own LC_ID_DYLIB, not a dependency.
                    continue
                links[binary][name] = library
                if library not in owners:
                    relative = 'lib/' + library.name
                    if relative in files and files[relative] != library:
                        raise BenchmarkError('python_layout_unsupported', 'Python requires conflicting library names.')
                    files[relative] = library
                    owners[library] = relative
                    queue.append(library)
        snapshot = root / 'runtime'
        origin = {}
        for name, source in sorted(files.items()):
            check_cancelled(cancel)
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            before = _digest(source)
            shutil.copyfile(source, target)
            target.chmod(0o755 if source in owners else 0o644)
            if _digest(target) != before:
                raise BenchmarkError('python_source_changed', 'Python changed during relocation; retry setup.')
            origin[name] = before
        for source, relative in owners.items():
            target = snapshot / relative
            replacements = []
            for original, library in links[source].items():
                local = snapshot / owners[library]
                replacement = '@loader_path/' + os.path.relpath(local, target.parent)
                replacements.extend(['-change', original, replacement])
            # Library IDs must not retain references to private host installations.
            if relative.startswith('lib/') and source.suffix != '.so':
                replacements.extend(['-id', '@rpath/' + source.name])
            if replacements:
                _tool(['/usr/bin/install_name_tool', *replacements, str(target)], root, cancel)
            _tool(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(target)], root, cancel)
        marker = snapshot / 'lib/prsim-python-origin.json'
        marker.write_text(json.dumps({'preparation': PREPARATION_VERSION, 'source_sha256': origin}, sort_keys=True))
        frozen = {path.relative_to(snapshot).as_posix(): path for path in snapshot.rglob('*') if path.is_file()}
        receipt = python_receipt(frozen, cancel)
        return publish_python(cache, frozen, receipt, cancel=cancel)
