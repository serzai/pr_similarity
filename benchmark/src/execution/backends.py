"""Explicit platform-bound sandbox selection; never choose an unsandboxed fallback."""

import platform
import shutil
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.seatbelt import SeatbeltSandbox


def validate_backend_host(backend: str) -> None:
    expected = {'bubblewrap': 'Linux', 'seatbelt': 'Darwin'}
    if backend not in expected or platform.system() != expected[backend]:
        raise BenchmarkError(
            'unsupported_sandbox_platform',
            'Choose sandbox.backend=seatbelt on macOS or bubblewrap on Linux.', exit_code=1,
        )


def resolve_seatbelt(executable: str) -> Path:
    validate_backend_host('seatbelt')
    found = shutil.which(executable)
    if found is None or Path(found).resolve() != Path('/usr/bin/sandbox-exec'):
        raise BenchmarkError(
            'sandbox_missing', 'Native macOS requires the system /usr/bin/sandbox-exec.', exit_code=1,
        )
    return Path(found).resolve()


def sandbox_type(backend: str) -> type[Sandbox]:
    if backend == 'bubblewrap':
        return Sandbox
    if backend == 'seatbelt':
        return SeatbeltSandbox
    raise BenchmarkError('invalid_sandbox', 'Unknown sandbox backend.')
