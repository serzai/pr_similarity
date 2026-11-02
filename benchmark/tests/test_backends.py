"""Explicit backend selection, host validation and immutable Linux defaults."""

from pathlib import Path

import pytest

from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.backends import resolve_seatbelt, sandbox_type, validate_backend_host
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.seatbelt import SeatbeltSandbox
from test_config import write_config


def test_native_backend_defaults_do_not_change_linux_defaults(tmp_path: Path) -> None:
    linux = load_config(write_config(tmp_path))
    native = load_config(write_config(tmp_path, sandbox={'backend': 'seatbelt'}))
    assert linux.data.sandbox.to_wire() == {'backend': 'bubblewrap', 'executable': 'bwrap', 'auto_install': True}
    assert native.data.sandbox.to_wire() == {
        'backend': 'seatbelt', 'executable': '/usr/bin/sandbox-exec', 'auto_install': False,
    }
    assert native.identity() == linux.identity()
    assert sandbox_type('bubblewrap') is Sandbox and sandbox_type('seatbelt') is SeatbeltSandbox


@pytest.mark.parametrize('settings', [
    {'backend': 'seatbelt', 'auto_install': True},
    {'backend': 'seatbelt', 'executable': '/bin/sh'},
    {'backend': 'unknown'},
])
def test_native_config_rejects_fallbacks(tmp_path: Path, settings: dict) -> None:
    with pytest.raises(BenchmarkError):
        load_config(write_config(tmp_path, sandbox=settings))


@pytest.mark.parametrize('backend, host', [('bubblewrap', 'Darwin'), ('seatbelt', 'Linux')])
def test_backend_host_mismatch_fails_before_issuance(monkeypatch: pytest.MonkeyPatch, backend: str, host: str) -> None:
    monkeypatch.setattr('benchmark.execution.backends.platform.system', lambda: host)
    with pytest.raises(BenchmarkError) as error:
        validate_backend_host(backend)
    assert error.value.code == 'unsupported_sandbox_platform'


def test_native_resolver_requires_system_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('benchmark.execution.backends.platform.system', lambda: 'Darwin')
    monkeypatch.setattr('benchmark.execution.backends.shutil.which', lambda _: '/private/tmp/sandbox-exec')
    with pytest.raises(BenchmarkError, match='system /usr/bin/sandbox-exec'):
        resolve_seatbelt('sandbox-exec')


def test_coordinator_checks_host_before_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmark.runtime import coordinator

    config = load_config(write_config(tmp_path, sandbox={'backend': 'seatbelt'}))
    session = object.__new__(coordinator.Coordinator)
    session.config = config
    monkeypatch.setattr('benchmark.execution.backends.platform.system', lambda: 'Linux')

    def forbidden(*_args: object, **_settings: object) -> None:
        raise AssertionError('No download or credentials before backend host validation')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    with pytest.raises(BenchmarkError) as error:
        session.prepare_agents()
    assert error.value.code == 'unsupported_sandbox_platform'
