"""Experimental deny-by-default macOS policy, independent of agent execution."""

import json
import platform
import threading
from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.execution.cancellation import check_cancelled
from benchmark.execution.paths import ExecutionPaths
from benchmark.execution.process import run_process
from benchmark.execution.python_sandbox import python_probe_script
from benchmark.execution.sandbox import Sandbox
from benchmark.models.runtime import PythonProbe, SandboxReceipt
from benchmark.persistence.storage import bytes_sha256

# No host HOME, /tmp, /usr/local or Homebrew prefix is implicitly readable.
_SYSTEM_READS = (
    '/System/Library', '/System/Cryptexes', '/System/Volumes/Preboot/Cryptexes',
    '/usr/bin', '/usr/lib', '/usr/libexec', '/usr/share', '/bin', '/sbin',
)
_PROFILE = '''(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow network-outbound (remote ip))
(allow network-outbound (literal "/private/var/run/mDNSResponder"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow mach-lookup (global-name "com.apple.SystemConfiguration.configd"))
(allow file-read-metadata ANCESTORS
  (literal "/etc") (literal "/private/etc") (literal "/private/etc/ssl")
  (literal "/private/var") (literal "/private/var/db") (literal "/private/var/select")
  (literal "/Library/Preferences"))
; dyld/libignition opens the root directory for openat, without granting subpaths.
(allow file-read* (literal "/") (literal "/System") (literal "/System/Volumes")
  (literal "/System/Volumes/Preboot"))
(allow file-read* file-map-executable
  SYSTEM_READS
  (literal (param "BINARY"))
  (literal (param "BINARY_ALIAS"))
  (subpath (param "SOURCE"))
  (subpath (param "SOURCE_ALIAS"))
  (subpath (param "HOME"))
  (subpath (param "HOME_ALIAS"))
  (subpath (param "TEMP"))
  (subpath (param "TEMP_ALIAS"))
  (subpath (param "RUNTIME"))
  (subpath (param "RUNTIME_ALIAS"))
  PYTHON_READS)
(allow file-write*
  (subpath (param "SOURCE"))
  (subpath (param "SOURCE_ALIAS"))
  (subpath (param "HOME"))
  (subpath (param "HOME_ALIAS"))
  (subpath (param "TEMP"))
  (subpath (param "TEMP_ALIAS")))
(deny file-write* (subpath (param "GIT")) (subpath (param "GIT_ALIAS")))
(allow file-read* (literal "/dev/null") (literal "/dev/zero")
  (literal "/dev/random") (literal "/dev/urandom"))
(allow file-write-data (literal "/dev/null"))
(allow file-read* (literal "/private/etc/resolv.conf") (literal "/private/etc/hosts")
  (literal "/private/etc/services") (literal "/private/etc/protocols")
  (literal "/private/etc/ssl/cert.pem") (literal "/private/etc/ssl/openssl.cnf")
  (subpath "/private/etc/ssl/certs") (subpath "/private/var/db/timezone")
  (literal "/private/var/select/sh")
  (literal "/Library/Preferences/com.apple.networkd.plist"))
(deny process-exec (literal "/usr/bin/python") (literal "/usr/bin/python3"))
'''


@dataclass(frozen=True)
class SeatbeltPolicy:
    """Describe only dedicated attempt paths; this is not yet a runnable backend."""

    source: Path
    home: Path
    temporary: Path
    runtime: Path
    binary: Path
    python_root: Path | None = None

    def __post_init__(self) -> None:
        """Reject overlapping writable roots that would expose private launcher state."""
        directories = [path.resolve() for path in (self.source, self.home, self.temporary, self.runtime)]
        for index, path in enumerate(directories):
            if path in (Path('/'), Path.home().resolve()):
                raise BenchmarkError('unsafe_sandbox', 'Seatbelt requires dedicated attempt directories.')
            if any(path.is_relative_to(other) or other.is_relative_to(path) for other in directories[:index]):
                raise BenchmarkError('unsafe_sandbox', 'Seatbelt attempt directories must not overlap.')
        binary = self.binary.resolve()
        if any(binary.is_relative_to(path) for path in directories[:3]):
            raise BenchmarkError('unsafe_sandbox', 'The pinned executable must stay outside writable attempt paths.')
        if self.python_root is not None:
            python = self.python_root.resolve()
            if python in (Path('/'), Path.home().resolve()) or any(
                python.is_relative_to(path) or path.is_relative_to(python) for path in directories[:3]
            ):
                raise BenchmarkError('unsafe_sandbox', 'The base Python must be private and readonly.')

    def profile(self) -> str:
        """Keep paths out of profile syntax by using sandbox-exec parameters."""
        system = '\n  '.join(f'(subpath "{path}")' for path in _SYSTEM_READS)
        ancestors = '\n  '.join(f'(literal (param "ANCESTOR_{index}"))' for index in range(len(self.ancestors())))
        python = '(subpath (param "PYTHON")) (subpath (param "PYTHON_ALIAS"))' if self.python_root else ''
        return _PROFILE.replace('SYSTEM_READS', system).replace('ANCESTORS', ancestors).replace('PYTHON_READS', python)

    def ancestors(self) -> list[Path]:
        """Permit only traversal metadata, without exposing arbitrary host file metadata."""
        roots = [*self.parameters().values(), *map(Path, _SYSTEM_READS)]
        return sorted({parent for root in roots for parent in root.parents})

    def parameters(self) -> dict[str, Path]:
        """Allow standard macOS aliases only for the exact dedicated roots."""
        result = {}
        paths = [
            ('SOURCE', self.source), ('HOME', self.home), ('TEMP', self.temporary),
            ('RUNTIME', self.runtime), ('BINARY', self.binary), ('GIT', self.source / '.git'),
        ]
        if self.python_root is not None:
            paths.append(('PYTHON', self.python_root))
        for key, path in paths:
            canonical = path.resolve()
            result[key] = canonical
            text = str(canonical)
            alias = text.removeprefix('/private') if text.startswith(('/private/var/', '/private/tmp/')) else text
            result[key + '_ALIAS'] = Path(alias)
        return result

    def command(self, argv: list[str]) -> list[str]:
        """Build a shell-free invocation; never fall back to a bare command."""
        if platform.system() != 'Darwin':
            raise BenchmarkError('unsupported_platform', 'Seatbelt requires macOS.', exit_code=1)
        args = ['/usr/bin/sandbox-exec']
        for key, path in self.parameters().items():
            args.extend(['-D', key + '=' + str(path)])
        for index, path in enumerate(self.ancestors()):
            args.extend(['-D', f'ANCESTOR_{index}={path}'])
        return [*args, '-p', self.profile(), *argv]


@dataclass(frozen=True)
class SeatbeltSandbox(Sandbox):
    """Native prototype; runner selection is gated on detached descendant cleanup."""

    def __post_init__(self) -> None:
        """Validate native paths before the base class creates or chmods any directory."""
        if not self.cwd.resolve().is_relative_to(self.source.resolve()):
            raise BenchmarkError('unsafe_sandbox', 'Sandbox cwd must stay inside the source snapshot.')
        self.policy()
        super().__post_init__()
        Path(self.paths.temporary).mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def paths(self) -> ExecutionPaths:
        """Use canonical attempt locations without pretending Seatbelt remaps paths."""
        return ExecutionPaths.native(
            self.source, self.cwd, self.home, self.runtime_files, self.binary, self.home.parent / 'temporary'
        )

    def policy(self) -> SeatbeltPolicy:
        """Always validate the complete layout before constructing a command."""
        return SeatbeltPolicy(
            self.source, self.home, Path(self.paths.temporary), self.runtime_files, self.binary,
            self.python.root if self.python is not None else None,
        )

    def command(self, argv: list[str]) -> list[str]:
        """Change cwd inside the restricted child, with arguments passed positionally."""
        return self.policy().command([
            '/bin/sh', '-c', 'cd "$1" && shift && exec "$@"', 'benchmark-launch', self.paths.cwd, *argv,
        ])

    def environment(
        self, bindings: dict[str, str], *, config_content: str | None = None, auth_content: str | None = None
    ) -> dict[str, str]:
        """Use only private process paths; never import the host process environment."""
        if any(key.startswith(('DYLD_', '__CF')) for key in bindings):
            raise BenchmarkError('invalid_environment', 'Bindings must not replace private launcher controls.')
        result = super().environment(bindings, config_content=config_content, auth_content=auth_content)
        home = self.paths.home
        result.update(HOME=home, TMPDIR=self.paths.temporary, LANG='en_US.UTF-8', LC_ALL='en_US.UTF-8')
        for key, child in (('CONFIG', 'config'), ('CACHE', 'cache'), ('DATA', 'data'), ('STATE', 'state')):
            result['XDG_' + key + '_HOME'] = home + '/' + child
        result['PATH'] = ':'.join((
            str(self.binary.resolve().parent), home + '/.local/bin', home + '/bin', '/usr/bin', '/bin',
        ))
        if self.python is not None:
            result['VIRTUAL_ENV'] = home + '/.venv'
            result['PATH'] = home + '/.venv/bin:' + result['PATH']
        return result

    def preflight(self, timeout: int = 10, *, cancel: threading.Event | None = None) -> SandboxReceipt:
        """Require a working positive control and blocked sibling access before use."""
        policy = self.policy()
        Path(self.paths.temporary).mkdir(parents=True, exist_ok=True, mode=0o700)
        forbidden = self.runtime_files.parent / 'seatbelt-preflight-private'
        if forbidden.exists() or forbidden.is_symlink():
            raise BenchmarkError('unsafe_sandbox', 'Sandbox probe path already exists.')
        forbidden.write_text('private probe')
        try:
            # A deny-only profile must not pass just because every command fails.
            for argv in (
                ['/bin/sh', '-c', 'printf ok > "$HOME/preflight" && cat "$HOME/preflight"'],
                ['/bin/sh', '-c', 'if cat "$1" >/dev/null 2>&1; then exit 1; fi', 'probe', str(forbidden.resolve())],
            ):
                result = run_process(
                    self.command(argv), env=self.environment({}), cwd=self.source,
                    timeout=timeout, grace=0.2, cancel=cancel or threading.Event(),
                    log=self.runtime_files / 'seatbelt-preflight.log',
                )
                if result.status == 'cancelled':
                    check_cancelled(cancel)
                if result.status != 'completed':
                    raise BenchmarkError(
                        'sandbox_unavailable', 'macOS Seatbelt enforcement is unavailable.', exit_code=1
                    )
        finally:
            forbidden.unlink()
        return SandboxReceipt(
            backend='seatbelt',
            version=platform.mac_ver()[0] + ':' + bytes_sha256(policy.profile().encode()),
            binary_sha256=bytes_sha256(Path('/usr/bin/sandbox-exec').read_bytes()),
            network='shared',
        )

    def prepare_python(
        self, *, timeout: int = 60, cancel: threading.Event | None = None, bindings: dict[str, str] | None = None
    ) -> PythonProbe:
        """Bootstrap one clean venv from the relocated, readonly setup interpreter."""
        if self.python is None:
            raise BenchmarkError('python_runtime_missing', 'An isolated Python runtime is required.')
        venv = self.home.resolve() / '.venv'
        if venv.exists() or venv.is_symlink():
            raise BenchmarkError('python_venv_exists', 'A fresh attempt requires a new Python environment.')
        self._python_process(
            [str(self.python.root / 'bin/python3.12'), '-I', '-B', '-m', 'venv', str(venv)],
            timeout=timeout, cancel=cancel, log=self.runtime_files / 'python-prepare.log',
        )
        # `env python*` shebangs use this venv. Protected absolute system shebangs fail closed.
        for name in ('python', 'python3', 'python3.12'):
            target = venv / 'bin' / name
            if not target.exists():
                target.symlink_to(venv / 'bin/python3.12')
        return self.check_python(timeout=timeout, cancel=cancel, bindings=bindings)

    def check_python(
        self, *, timeout: int = 30, cancel: threading.Event | None = None, bindings: dict[str, str] | None = None
    ) -> PythonProbe:
        """Validate actual native locations, then emit portable, versioned identifiers."""
        if self.python is None:
            raise BenchmarkError('python_runtime_missing', 'An isolated Python runtime is required.')
        venv = str(self.home.resolve() / '.venv')
        base = str(self.python.root.resolve())
        path = self.runtime_files / 'python-probe.py'
        path.write_text(python_probe_script(base, venv, native=True))
        lines = self._python_process(
            [venv + '/bin/python', str(path.resolve())], timeout=timeout, cancel=cancel,
            log=self.runtime_files / 'python-check.log', bindings=bindings,
        )
        probe = PythonProbe.model_validate(json.loads('\n'.join(lines)))
        if probe.version != self.python.receipt.version or probe.schema_version != 2:
            raise BenchmarkError('python_preflight_failed', 'Native Python differs from its frozen runtime.')
        return probe
