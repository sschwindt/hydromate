"""Is that process still there? - the primitives, without ``psutil``.

A detached job records a PID. Some time later, possibly after a reboot, something has to
decide whether that PID still means what it meant. Three facts are needed and none of
them is a plain ``os.kill(pid, 0)``:

* **PID reuse.** On Linux, PIDs wrap; on a busy box a multi-day job's PID can be
  reassigned to something unrelated. ``os.kill(pid, 0)`` then says "alive" about a
  process that is not the job. So the *start time* is recorded alongside the PID and
  compared - the pair is what identifies a process, not the number alone.
* **Reboot.** After a restart every recorded PID is meaningless. The boot id makes that
  a single comparison rather than a heuristic, and it is also the ``wsl --shutdown``
  case: the distro's VM restarted and every job in it is gone.
* **Another machine.** A job root on a shared filesystem may hold jobs launched
  elsewhere. Their PIDs say nothing here, so the host is recorded and a foreign job is
  reported rather than judged.

``psutil`` would supply all three, but the dependency policy (plan §28) is to stay light,
and this is ~100 lines of standard library.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

__all__ = ["boot_id", "host_id", "pid_alive", "process_start_time", "self_start_time"]


def host_id() -> str:
    """A stable-enough machine name. Used only to refuse to judge a foreign job."""
    try:
        return socket.gethostname()
    except OSError:            # pragma: no cover - a hostname-less container
        return "unknown-host"


def boot_id() -> str | None:
    """An identifier that changes on every boot, or ``None`` if unobtainable.

    Linux exposes one directly. Elsewhere it is derived from the boot *instant*, rounded
    hard: the point is only to differ across restarts, and monotonic-clock jitter must
    not make two readings on the same boot disagree.
    """
    linux = Path("/proc/sys/kernel/random/boot_id")
    try:
        if linux.is_file():
            return linux.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        booted = time.time() - time.monotonic()
    except (OSError, ValueError):   # pragma: no cover
        return None
    return f"boot-{int(booted // 60)}"      # minute resolution: coarse on purpose


def pid_alive(pid: int | None) -> bool:
    """True when *pid* names a live process. Never raises.

    **A zombie counts as dead**, and getting this wrong is not academic. ``os.kill(pid,
    0)`` succeeds against a zombie, because the process table entry still exists until
    the parent reaps it. So a job whose runner has exited but whose parent has not yet
    called ``wait`` reads as RUNNING - which makes ``cancel`` report that it failed to
    stop something that already stopped, and leaves the dashboard waiting forever. In
    production the launcher's parent exits and init reaps the child, so the window is
    short; in any long-lived parent (a test runner, a plugin process that submitted) it
    is not.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(int(pid))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, which still means the PID is in use - and a foreign
        # owner is precisely not a dead job.
        return True
    except OSError:             # pragma: no cover
        return False
    return not _is_zombie(int(pid))


def _is_zombie(pid: int) -> bool:
    """Linux: field 3 of ``/proc/<pid>/stat`` is the state; ``Z`` is a reaped-pending
    corpse. Elsewhere this cannot be told cheaply, so it reports False."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    close = raw.rfind(")")
    if close < 0:
        return False
    parts = raw[close + 2:].split()
    return bool(parts) and parts[0] == "Z"


def process_start_time(pid: int | None) -> float | None:
    """When *pid* started, in whatever unit the platform offers.

    The value is only ever compared with another reading from the same platform, so the
    unit does not need to be portable - only stable. ``None`` means "cannot tell", which
    callers must treat as "do not conclude anything", not as "changed".
    """
    if not pid or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        return _linux_start_time(int(pid))
    if os.name == "nt":
        return _windows_start_time(int(pid))
    return _posix_start_time(int(pid))


def self_start_time() -> float | None:
    return process_start_time(os.getpid())


# ----------------------------------------------------------------------- platforms


def _linux_start_time(pid: int) -> float | None:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks since boot.

    Parsed from the last ``)`` rather than by splitting the whole line, because field 2
    is the executable name in parentheses and may itself contain spaces and brackets -
    a splitter that does not know this silently mis-indexes every later field.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    parts = raw[close + 2:].split()
    try:
        return float(parts[19])          # field 22 overall, 20th after the comm field
    except (IndexError, ValueError):     # pragma: no cover
        return None


def _posix_start_time(pid: int) -> float | None:
    """macOS/BSD: ask ``ps`` for the elapsed time, and derive the start instant."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):     # pragma: no cover
        return None
    text = out.stdout.strip()
    if not text:
        return None
    return float(abs(hash(text)) % (10 ** 12))        # stable per (pid, start), which is all
                                                      # the comparison needs


def _windows_handles():        # pragma: no cover - exercised only on Windows
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    return ctypes, wintypes, kernel32


#: ``PROCESS_QUERY_LIMITED_INFORMATION`` - the least privilege that answers both
#: questions, and the one that works across a session boundary.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _windows_pid_alive(pid: int) -> bool:      # pragma: no cover - Windows only
    ctypes, wintypes, kernel32 = _windows_handles()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        # A process that genuinely exited with 259 is indistinguishable here. That is a
        # documented Win32 wart; the start-time comparison above is what disambiguates.
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_start_time(pid: int) -> float | None:   # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    ctypes_mod, wintypes_mod, kernel32 = _windows_handles()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_t = wintypes.FILETIME()
        kernel_t = wintypes.FILETIME()
        user_t = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                      ctypes.byref(kernel_t), ctypes.byref(user_t))
        if not ok:
            return None
        return float((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)
