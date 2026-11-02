"""Exact release selection, checked archives and cache recovery without network."""

import asyncio
import hashlib
import io
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from benchmark.errors import BenchmarkError
from benchmark.execution import provisioning
from benchmark.execution.provisioning import extract_binary, probe_version, provision_opencode, select_asset
from benchmark.persistence.storage import file_lock


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep existing Linux fixtures independent of the machine running the tests."""
    monkeypatch.setattr(provisioning.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(provisioning.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(provisioning.platform, 'libc_ver', lambda: ('glibc', '2.36'))


@pytest.mark.parametrize(
    ('system', 'machine', 'libc', 'expected'),
    [
        ('Linux', 'x86_64', 'glibc', ('opencode-linux-x64-baseline.tar.gz', 'linux-x64-glibc-baseline')),
        ('Linux', 'amd64', 'glibc', ('opencode-linux-x64-baseline.tar.gz', 'linux-x64-glibc-baseline')),
        ('Darwin', 'arm64', '', ('opencode-darwin-arm64.zip', 'darwin-arm64')),
        ('Darwin', 'x86_64', '', ('opencode-darwin-x64-baseline.zip', 'darwin-x64-baseline')),
    ],
)
def test_platform_selects_official_asset(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, libc: str, expected: tuple[str, str]
) -> None:
    """Select native assets without requiring glibc on macOS."""
    monkeypatch.setattr(provisioning.platform, 'system', lambda: system)
    monkeypatch.setattr(provisioning.platform, 'machine', lambda: machine)
    monkeypatch.setattr(provisioning.platform, 'libc_ver', lambda: (libc, ''))
    assert select_asset() == expected


@pytest.mark.parametrize(
    ('system', 'machine', 'libc'),
    [
        ('Linux', 'x86_64', 'musl'),
        ('Linux', 'aarch64', 'glibc'),
        ('Darwin', 'ppc', ''),
        ('Windows', 'AMD64', ''),
    ],
)
def test_unsupported_platform_fails_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, machine: str, libc: str
) -> None:
    """Do not pick a binary for an unsupported architecture or libc."""
    monkeypatch.setattr(provisioning.platform, 'system', lambda: system)
    monkeypatch.setattr(provisioning.platform, 'machine', lambda: machine)
    monkeypatch.setattr(provisioning.platform, 'libc_ver', lambda: (libc, ''))
    client, calls = client_for(archive())
    with pytest.raises(BenchmarkError) as caught:
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert caught.value.code == 'unsupported_platform'
    assert calls == []
    assert not list(tmp_path.iterdir())


def archive(name: str = 'opencode', body: bytes = b'#!/bin/sh\nprintf "1.18.33\\n"\n') -> bytes:
    """Build an in-memory release archive containing a configurable executable."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        info = tarfile.TarInfo(name)
        info.mode = 0o755
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return stream.getvalue()


def zip_archive(entries: list[tuple[str, bytes, int]]) -> bytes:
    """Create ZIP fixtures with explicit Unix file types, including malicious entries."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as source:
        for name, body, mode in entries:
            item = zipfile.ZipInfo(name)
            item.create_system = 3
            item.external_attr = mode << 16
            source.writestr(item, body)
    return stream.getvalue()


def client_for(
    content: bytes, *, expected_digest: str | None = None, asset_name: str = 'opencode-linux-x64-baseline.tar.gz'
) -> tuple[httpx.AsyncClient, list[str]]:
    """Create a mock release client and its ordered request-path history."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Serve a pinned release manifest and executable archive from memory."""
        calls.append(request.url.path)
        if '/releases/tags/' in request.url.path:
            return httpx.Response(
                200,
                json={
                    'id': 1,
                    'tag_name': 'v1.18.33',
                    'assets': [
                        {
                            'id': 2,
                            'name': asset_name,
                            'size': len(content),
                            'digest': expected_digest or 'sha256:' + hashlib.sha256(content).hexdigest(),
                            'browser_download_url': (
                                'https://github.com/anomalyco/opencode/releases/download/'
                                f'v1.18.33/{asset_name}'
                            ),
                        }
                    ],
                },
            )
        return httpx.Response(200, content=content)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond)), calls


@pytest.mark.parametrize(
    ('machine', 'asset_name', 'platform_name'),
    [
        ('arm64', 'opencode-darwin-arm64.zip', 'darwin-arm64'),
        ('x86_64', 'opencode-darwin-x64-baseline.zip', 'darwin-x64-baseline'),
    ],
)
def test_darwin_zip_download_probe_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine: str, asset_name: str, platform_name: str
) -> None:
    """A native ZIP follows the same checked cache lifecycle as the Linux tarball."""
    monkeypatch.setattr(provisioning.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(provisioning.platform, 'machine', lambda: machine)
    content = zip_archive([('opencode', b'#!/bin/sh\nprintf "1.18.33\\n"\n', stat.S_IFREG | 0o755)])
    client, calls = client_for(content, asset_name=asset_name)
    first = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    second = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert first.binary == tmp_path / 'opencode/v1.18.33' / platform_name / 'opencode'
    assert first == second
    assert first.receipt.asset_name == asset_name
    assert first.receipt.platform == platform_name
    assert first.receipt.actual_version == '1.18.33'
    assert first.receipt.digest_verified is True
    assert first.receipt.archive_sha256 == hashlib.sha256(content).hexdigest()
    assert stat.S_IMODE(first.binary.stat().st_mode) == 0o700
    assert calls == [
        '/repos/anomalyco/opencode/releases/tags/v1.18.33',
        f'/anomalyco/opencode/releases/download/v1.18.33/{asset_name}',
    ]
    first.binary.write_bytes(b'corrupt cache')
    restored = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert restored.receipt.binary_sha256 == first.receipt.binary_sha256
    assert len(calls) == 4


@pytest.mark.parametrize('failure', ['digest', 'version'])
def test_darwin_zip_rejects_unverified_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """ZIP support must not bypass archive integrity or the native version probe."""
    monkeypatch.setattr(provisioning.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(provisioning.platform, 'machine', lambda: 'arm64')
    content = zip_archive([('opencode', b'#!/bin/sh\necho 1.2.3\n', stat.S_IFREG | 0o755)])
    client, _calls = client_for(
        content,
        asset_name='opencode-darwin-arm64.zip',
        expected_digest='sha256:' + 'a' * 64 if failure == 'digest' else None,
    )
    with pytest.raises(BenchmarkError) as caught:
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert caught.value.code == f'runtime_{failure}_mismatch'
    assert not (tmp_path / 'opencode/v1.18.33/darwin-arm64/opencode').exists()
    assert not (tmp_path / 'opencode/v1.18.33/darwin-arm64/receipt.json').exists()


@pytest.mark.parametrize(
    ('name', 'mode'),
    [
        ('../opencode', stat.S_IFREG),
        ('/tmp/opencode', stat.S_IFREG),
        ('a/../opencode', stat.S_IFREG),
        ('a//opencode', stat.S_IFREG),
        ('opencode', stat.S_IFLNK),
        ('opencode', stat.S_IFIFO),
        ('unrelated-link', stat.S_IFLNK),
    ],
)
def test_zip_rejects_unsafe_members_before_writing(tmp_path: Path, name: str, mode: int) -> None:
    """Unsafe entries invalidate the whole archive, even outside the selected binary."""
    asset = tmp_path / 'asset.zip'
    asset.write_bytes(zip_archive([
        ('nested/opencode', b'CLI', stat.S_IFREG | 0o755),
        (name, b'target', mode | 0o755),
    ]))
    destination = tmp_path / 'binary'
    with pytest.raises(BenchmarkError) as caught:
        extract_binary(asset, destination)
    assert caught.value.code == 'runtime_archive_unsafe'
    assert not destination.exists()


@pytest.mark.parametrize('names', [['README'], ['opencode', 'nested/opencode'], ['opencode/']])
def test_zip_requires_one_regular_binary(tmp_path: Path, names: list[str]) -> None:
    """Missing, duplicate and directory-only candidates cannot become runtime executables."""
    asset = tmp_path / 'asset.zip'
    asset.write_bytes(zip_archive([
        (name, b'body', stat.S_IFDIR if name.endswith('/') else stat.S_IFREG) for name in names
    ]))
    with pytest.raises(BenchmarkError) as caught:
        extract_binary(asset, tmp_path / 'binary')
    assert caught.value.code == 'runtime_binary_missing'
    assert not (tmp_path / 'binary').exists()


def test_corrupt_zip_is_a_runtime_error(tmp_path: Path) -> None:
    """Broken ZIP data must not leak zipfile exceptions to the runner."""
    asset = tmp_path / 'asset.zip'
    asset.write_bytes(b'not a zip')
    with pytest.raises(BenchmarkError) as caught:
        extract_binary(asset, tmp_path / 'binary')
    assert caught.value.code == 'runtime_archive_failed'


def test_nested_zip_member_is_copied_without_extracting_its_path(tmp_path: Path) -> None:
    """Only the chosen executable is written to the caller-owned destination."""
    asset = tmp_path / 'asset.zip'
    asset.write_bytes(zip_archive([
        ('nested/opencode', b'CLI', stat.S_IFREG | 0o755),
        ('README', b'ignored', stat.S_IFREG | 0o644),
    ]))
    destination = tmp_path / 'binary'
    extract_binary(asset, destination)
    assert destination.read_bytes() == b'CLI'
    assert not (tmp_path / 'nested').exists()
    assert not (tmp_path / 'README').exists()


def test_checked_cache_does_not_repeat_download(tmp_path: Path) -> None:
    """Verify checked cache does not repeat download."""
    client, calls = client_for(archive())
    one = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    two = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert one.binary == two.binary
    assert one.receipt.actual_version == '1.18.33'
    assert one.receipt.digest_verified is True
    assert len(calls) == 2


def test_corrupt_cached_binary_is_replaced(tmp_path: Path) -> None:
    """Verify corrupt cached binary is replaced."""
    client, calls = client_for(archive())
    one = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    one.binary.write_text('corrupted')
    two = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert two.binary.read_bytes().startswith(b'#!/bin/sh')
    assert len(calls) == 4


@pytest.mark.parametrize('name', ['../opencode', '/tmp/opencode'])
def test_unsafe_archive_never_installs(tmp_path: Path, name: str) -> None:
    """Verify unsafe archive never installs."""
    client, _calls = client_for(archive(name))
    with pytest.raises(BenchmarkError):
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert not [path for path in tmp_path.rglob('opencode') if path.is_file()]


def test_published_digest_mismatch_fails_before_version_probe(tmp_path: Path) -> None:
    """Verify published digest mismatch fails before version probe."""
    client, _calls = client_for(archive(), expected_digest='sha256:' + 'a' * 64)
    with pytest.raises(BenchmarkError) as caught:
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert caught.value.code == 'runtime_digest_mismatch'


def test_actual_version_mismatch_is_not_cached(tmp_path: Path) -> None:
    """Verify actual version mismatch is not cached."""
    client, _calls = client_for(archive(body=b'#!/bin/sh\necho 1.2.3\n'))
    with pytest.raises(BenchmarkError) as caught:
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert caught.value.code == 'runtime_version_mismatch'


@pytest.mark.parametrize('selector', ['opencode:latest', 'opencode:v1.x.y', 'opencode:>=1.0', 'opencode:v1.2'])
def test_runtime_is_exact(selector: str, tmp_path: Path) -> None:
    """Verify runtime is exact."""
    with pytest.raises(BenchmarkError):
        provision_opencode(selector, tmp_path)


def test_relative_cache_is_resolved_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify relative cache is resolved once."""
    monkeypatch.chdir(tmp_path)
    client, calls = client_for(archive())
    one = provision_opencode('opencode:v1.18.33', Path('relative-cache'), client=client)
    two = provision_opencode('opencode:v1.18.33', Path('relative-cache'), client=client)
    assert one.binary.is_absolute() and one.binary == two.binary
    assert len(calls) == 2


def test_cancelled_runtime_cache_wait_does_not_wait_for_timeout(tmp_path: Path) -> None:
    """Cancel a real held cache lock promptly without reporting infrastructure timeout."""
    directory = tmp_path / 'opencode/v1.18.33/linux-x64-glibc-baseline'
    directory.mkdir(parents=True)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    with file_lock(directory / 'install.lock'):
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(BenchmarkError) as caught:
                provision_opencode('opencode:v1.18.33', tmp_path, timeout=120, cancel=cancel)
            assert caught.value.code == 'operation_cancelled'
            assert time.monotonic() - started < 1
        finally:
            timer.join()


def test_cancelled_runtime_request_reaps_owned_async_operation(tmp_path: Path) -> None:
    """Abort an in-flight mock request and await its cleanup before returning cancellation."""
    cancel = threading.Event()
    cleaned = threading.Event()

    async def slow(_request: httpx.Request) -> httpx.Response:
        """Simulate bounded startup I/O that must be cancelled before completion."""
        try:
            await asyncio.sleep(30)
            raise AssertionError('startup request should have been cancelled')
        finally:
            cleaned.set()

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(BenchmarkError) as caught:
            provision_opencode('opencode:v1.18.33', tmp_path, timeout=120, client=client, cancel=cancel)
        assert caught.value.code == 'operation_cancelled'
        assert cleaned.is_set() and time.monotonic() - started < 1
    finally:
        timer.join()
        asyncio.run(client.aclose())


def test_cancelled_native_version_probe_terminates_process(tmp_path: Path) -> None:
    """Cancel an actual native process instead of waiting for the startup probe deadline."""
    binary = tmp_path / 'probe'
    binary.write_text('#!/bin/sh\nsleep 30\n')
    binary.chmod(0o700)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(BenchmarkError) as caught:
            probe_version(binary, 120, cancel=cancel)
        assert caught.value.code == 'operation_cancelled'
        assert time.monotonic() - started < 2
    finally:
        timer.join()
