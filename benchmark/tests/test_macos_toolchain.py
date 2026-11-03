"""Measure readonly compiler inputs without following SDK symlink cycles."""

import os
import platform
import subprocess
from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.execution.macos_toolchain import prepare_macos_toolchain, toolchain_digest
from benchmark.execution.seatbelt import SeatbeltPolicy


def test_digest_measures_content_names_and_internal_links(tmp_path: Path) -> None:
    root = tmp_path / 'SDK'
    root.mkdir()
    (root / 'header.h').write_text('int value;')
    (root / 'cycle').symlink_to('.')
    before = toolchain_digest(root)
    assert toolchain_digest(root) == before
    (root / 'header.h').write_text('int changed;')
    assert toolchain_digest(root) != before
    before = toolchain_digest(root)
    (root / 'header.h').rename(root / 'renamed.h')
    assert toolchain_digest(root) != before


@pytest.mark.parametrize('target', ['/usr/include', '../outside'])
def test_digest_rejects_links_outside_snapshot(tmp_path: Path, target: str) -> None:
    (tmp_path / 'link').symlink_to(target)
    with pytest.raises(BenchmarkError, match='escapes'):
        toolchain_digest(tmp_path)


def test_digest_rejects_special_files(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / 'pipe')
    with pytest.raises(BenchmarkError, match='special file'):
        toolchain_digest(tmp_path)


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Apple SDK native integration')
def test_private_sdk_compiles_cpp_without_exposing_host_toolchain(tmp_path: Path) -> None:
    toolchain = prepare_macos_toolchain(tmp_path / 'cache')
    paths = [tmp_path / name for name in ('source', 'home', 'temp', 'runtime')]
    for path in paths:
        path.mkdir()
    source, home, temporary, runtime = paths
    policy = SeatbeltPolicy(source, home, temporary, runtime, Path('/bin/sh'), toolchain_root=toolchain)
    program = source / 'probe.cpp'
    program.write_text('#include <vector>\nint main(){std::vector<int> v{1,2};return v.size()!=2;}\n')
    environment = {'HOME': str(home), 'TMPDIR': str(temporary), 'PATH': str(toolchain / 'bin') + ':/usr/bin:/bin',
                   'SDKROOT': str(toolchain / 'sdk')}
    def run(argv):
        return subprocess.run(policy.command(argv), cwd=source, env=environment, capture_output=True,
                              text=True, timeout=60, check=False)
    compiled = run([str(toolchain / 'bin/clang++'), '-std=c++17', str(program), '-o', str(home / 'probe')])
    assert compiled.returncode == 0, compiled.stderr
    assert run([str(home / 'probe')]).returncode == 0
    assert run(['/bin/sh', '-c', 'echo corrupt > "$1"', 'probe', str(toolchain / 'bin/clang')]).returncode != 0
    # A separately selected original SDK path has not become a readable root.
    original = subprocess.check_output(['/usr/bin/xcrun', '--show-sdk-path'], text=True).strip()
    assert run(['/bin/cat', original + '/usr/include/stdio.h']).returncode != 0
    assert toolchain_digest(toolchain) == toolchain.name
