"""Backend-owned process paths, separate from host artifact storage paths."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutionPaths:
    """Name only paths visible to the launched process, never arbitrary host mounts."""

    source: str
    cwd: str
    home: str
    runtime: str
    binary: str
    temporary: str

    @classmethod
    def bubblewrap(cls, source: Path, cwd: Path) -> 'ExecutionPaths':
        """Preserve the existing Linux namespace and relative workspace selection."""
        relative = cwd.resolve().relative_to(source.resolve()).as_posix()
        return cls(
            '/repo', '/repo' if relative == '.' else '/repo/' + relative,
            '/sandbox-home', '/benchmark-runtime', '/benchmark-bin/opencode', '/tmp',
        )

    @classmethod
    def native(
        cls, source: Path, cwd: Path, home: Path, runtime: Path, binary: Path, temporary: Path
    ) -> 'ExecutionPaths':
        """Seatbelt restricts canonical host paths and does not provide bind mounts."""
        cwd.resolve().relative_to(source.resolve())
        return cls(*(str(path.resolve()) for path in (source, cwd, home, runtime, binary, temporary)))

    def runtime_file(self, relative: str) -> str:
        """Resolve launcher-owned resource names without source-dependent expansion."""
        if not relative or any(part in ('', '.', '..') for part in relative.split('/')):
            raise ValueError('Runtime resource path must be relative and normalized.')
        return self.runtime + '/' + relative
