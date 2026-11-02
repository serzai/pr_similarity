"""Kernel process identities and coalition-scoped native teardown."""

import ctypes
import errno
import os
import signal
import time
from dataclasses import dataclass

from benchmark.errors import BenchmarkError


# ABI: Apple XNU bsd/sys/proc_info_private.h and bsd/sys/proc_info.h.
# libproc returns errno directly for proc_signal_with_audittoken (signal 0 is invalid).
class _UniqueInfo(ctypes.Structure):
    _fields_ = [
        ('uuid', ctypes.c_ubyte * 16), ('unique', ctypes.c_uint64), ('parent', ctypes.c_uint64),
        ('version', ctypes.c_int32), ('parent_version', ctypes.c_int32),
        ('reserved', ctypes.c_uint64 * 2),
    ]


class _ShortInfo(ctypes.Structure):
    _fields_ = [
        ('pid', ctypes.c_uint32), ('ppid', ctypes.c_uint32), ('pgid', ctypes.c_uint32),
        ('status', ctypes.c_uint32), ('name', ctypes.c_char * 16), ('flags', ctypes.c_uint32),
        ('uid', ctypes.c_uint32), ('gid', ctypes.c_uint32), ('ruid', ctypes.c_uint32),
        ('rgid', ctypes.c_uint32), ('svuid', ctypes.c_uint32), ('svgid', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    version: int
    unique: int
    coalition: int
    status: int


class NativeProcesses:
    """Use a kernel PID version for signals, never a bare PID obtained by polling."""

    def __init__(self) -> None:
        try:
            self.library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.library.proc_signal_with_audittoken
        except (OSError, AttributeError) as exc:
            raise BenchmarkError(
                'native_cleanup_unavailable', 'This macOS installation lacks safe native process signalling.'
            ) from exc
        self.library.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        self.library.proc_pidinfo.restype = ctypes.c_int
        self.library.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        self.library.proc_listpids.restype = ctypes.c_int
        self.library.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.library.proc_signal_with_audittoken.restype = ctypes.c_int

    def _read(self, pid: int, flavor: int, output: ctypes.Structure | ctypes.Array) -> bool:
        copied = self.library.proc_pidinfo(pid, flavor, 0, ctypes.byref(output), ctypes.sizeof(output))
        return copied == ctypes.sizeof(output)

    def identity(self, pid: int) -> ProcessIdentity | None:
        unique, verified = _UniqueInfo(), _UniqueInfo()
        coalition = (ctypes.c_uint64 * 5)()
        info = _ShortInfo()
        if not self._read(pid, 17, unique):
            return None
        if not self._read(pid, 20, coalition) or not self._read(pid, 13, info) or not self._read(pid, 17, verified):
            return None
        if unique.unique != verified.unique or unique.version != verified.version or info.uid != os.getuid():
            return None
        return ProcessIdentity(pid, unique.version, unique.unique, coalition[0], info.status)

    def members(self, coalition: int) -> list[ProcessIdentity]:
        if coalition <= 0:
            raise BenchmarkError('unsafe_process_cleanup', 'A private native coalition is required.')
        # Enumerate only the current user's processes; never read their arguments or environment.
        needed = self.library.proc_listpids(4, os.getuid(), None, 0)
        for _ in range(3):
            buffer = (ctypes.c_int * (max(needed // 4, 1) + 128))()
            copied = self.library.proc_listpids(4, os.getuid(), ctypes.byref(buffer), ctypes.sizeof(buffer))
            if copied <= 0:
                raise BenchmarkError('native_cleanup_unavailable', 'Cannot enumerate native process ownership.')
            if copied < ctypes.sizeof(buffer):
                result = []
                for pid in buffer[:copied // 4]:
                    if pid > 0 and (identity := self.identity(pid)) is not None and identity.coalition == coalition:
                        result.append(identity)
                return result
            needed = copied * 2
        raise BenchmarkError('native_cleanup_unavailable', 'Native process inventory keeps changing.')

    def send(self, identity: ProcessIdentity, number: int) -> bool:
        current = self.identity(identity.pid)
        if current is None or (current.unique, current.version, current.coalition) != (
            identity.unique, identity.version, identity.coalition
        ):
            return False
        token = (ctypes.c_uint32 * 8)()
        token[5], token[7] = identity.pid, identity.version & 0xffffffff
        result = self.library.proc_signal_with_audittoken(ctypes.byref(token), number)
        if result in (errno.ESRCH, errno.ENOENT):
            return False
        if result != 0:
            raise BenchmarkError('native_cleanup_unavailable', 'Cannot signal an owned native process safely.')
        return True

    def _active(self, coalition: int, guardian: ProcessIdentity | None) -> list[ProcessIdentity]:
        excluded = (guardian.pid, guardian.version, guardian.unique) if guardian is not None else None
        return [
            item for item in self.members(coalition)
            if item.status != 5 and (item.pid, item.version, item.unique) != excluded
        ]  # SZOMB cannot execute or fork.

    def cleanup(self, coalition: int, *, guardian: ProcessIdentity | None = None, timeout: float = 5) -> None:
        """Freeze the complete coalition before killing it, including detached grandchildren."""
        own = self.identity(os.getpid())
        authorized_guardian = (
            own is not None and guardian is not None
            and (own.pid, own.version, own.unique, own.coalition)
            == (guardian.pid, guardian.version, guardian.unique, guardian.coalition)
        )
        if own is None or (guardian is not None and not authorized_guardian) or (
            own.coalition == coalition and not authorized_guardian
        ):
            raise BenchmarkError('unsafe_process_cleanup', 'Refusing to clean the caller coalition.')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = self._active(coalition, guardian)
            if not active:
                return
            for item in active:
                self.send(item, signal.SIGSTOP)
            frozen = self._active(coalition, guardian)
            # Only after every remaining process is stopped can the set no longer grow.
            if all(item.status == 4 for item in frozen):  # SSTOP
                for item in frozen:
                    self.send(item, signal.SIGKILL)
            time.sleep(0.01)
        raise BenchmarkError('native_cleanup_failed', 'Owned native processes did not terminate.')
