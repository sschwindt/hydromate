"""Starting a job that outlives whatever started it.

This is the interface plan §8 asks for, and the reason it is an interface rather than one
implementation is that "detached" means four genuinely different things:

* **systemd user services** on Linux, where the kernel already provides the thing this
  module wants - a named unit whose whole cgroup can be queried, killed and logged as a
  unit. MPI ranks belong to it automatically.
* **A new POSIX session** everywhere else, where the process group is the unit.
* **A Windows Job Object**, because Windows has no process groups in the POSIX sense and
  without a Job Object there is no reliable tree-kill at all.
* **WSL**, where the job must be detached *inside* the distro and the Windows side acts
  on it only by running commands through ``wsl.exe``.

Two rules cut across all four:

**The environment goes on the process, not into a wrapper shell.** Every launcher takes
an already-captured environment dict (:meth:`SolverEnvironment.capture`) and sets it
directly. A ``bash -lc 'source ...; hydromate execute'`` in the middle would add a layer
between the unit and the ranks, and each such layer is somewhere a signal can fail to
propagate.

**A handle must survive the launching process.** Everything needed to query or cancel the
job later - unit name, pid, pgid, job-object name, the recording host and boot id - is
serialised into ``status.json``. A fresh ``hydromate cancel`` days later reconstructs the
launcher from that alone.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hydromate.core.environment import EnvironmentKind, SolverEnvironment
from hydromate.core.errors import ConfigError
from hydromate.jobs import procs
from hydromate.jobs.model import utc_now

log = logging.getLogger("hydromate.jobs.launcher")

__all__ = ["JobLauncher", "LaunchHandle", "LaunchState", "available_launchers",
           "launcher_for", "select_launcher"]


class LaunchState(str, Enum):
    """What the launcher can say about a handle.

    ``UNKNOWN`` is a real answer and is treated as "still running" by the reaper.
    Reporting a job dead because its supervisor was momentarily unreachable would be
    worse than saying nothing.
    """

    RUNNING = "running"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass
class LaunchHandle:
    """Everything needed to find the job again from a different process."""

    backend: str
    pid: int | None = None
    pgid: int | None = None
    proc_started: float | None = None
    unit: str | None = None
    job_object: str | None = None
    distro: str | None = None
    host: str = field(default_factory=procs.host_id)
    boot_id: str | None = field(default_factory=procs.boot_id)
    started: str = field(default_factory=utc_now)
    log_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "pid": self.pid, "pgid": self.pgid,
                "proc_started": self.proc_started, "unit": self.unit,
                "job_object": self.job_object, "distro": self.distro,
                "host": self.host, "boot_id": self.boot_id, "started": self.started,
                "log_path": self.log_path}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LaunchHandle":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in dict(data or {}).items() if k in known})


@runtime_checkable
class JobLauncher(Protocol):
    name: str

    @classmethod
    def available(cls, env: SolverEnvironment | None = None) -> bool:
        """Can this launcher be used on this machine, right now?"""
        ...

    def submit(self, job_dir: Any, argv: list[str], *, env: dict[str, str],
               cwd: Path | str) -> LaunchHandle:
        ...

    def status(self, handle: LaunchHandle) -> LaunchState:
        ...

    def cancel(self, handle: LaunchHandle, *, grace: float = 20.0) -> bool:
        ...

    def logs(self, handle: LaunchHandle) -> Path | None:
        ...


# ------------------------------------------------------------------------ selection

#: Env override, so a machine with a broken user manager can be told to skip systemd
#: without editing a profile.
ENV_LAUNCHER = "HYDROMATE_LAUNCHER"


def _registry() -> dict[str, type]:
    from hydromate.jobs.launchers.posix import PosixDetachedLauncher
    from hydromate.jobs.launchers.systemd import SystemdUserLauncher
    from hydromate.jobs.launchers.windows import WindowsJobObjectLauncher
    from hydromate.jobs.launchers.wsl import WslLauncher
    return {"systemd": SystemdUserLauncher, "posix": PosixDetachedLauncher,
            "windows": WindowsJobObjectLauncher, "wsl": WslLauncher}


def available_launchers(env: SolverEnvironment | None = None) -> list[str]:
    """Which launchers this machine can actually use. For ``hydromate profiles``."""
    return [name for name, cls in _registry().items() if cls.available(env)]


def select_launcher(env: SolverEnvironment | None = None, *,
                    prefer: str | None = None) -> JobLauncher:
    """Pick a launcher, first hit wins.

    The WSL rule is a **forced** match rather than a preference: a captured Linux
    environment cannot start a Windows process, so a WSL solver has exactly one way to
    be launched and letting a preference override it would produce a job that cannot
    run.
    """
    registry = _registry()
    prefer = prefer or os.environ.get(ENV_LAUNCHER) or None

    if env is not None and env.kind is EnvironmentKind.WSL:
        if prefer and prefer not in ("auto", "wsl"):
            log.warning("ignoring launcher %r: a wsl environment must be launched "
                        "inside the distro", prefer)
        return registry["wsl"]()

    if prefer and prefer != "auto":
        cls = registry.get(prefer)
        if cls is None:
            raise ConfigError(
                f"unknown launcher {prefer!r}",
                subject="launcher",
                remedy="Use one of: auto, " + ", ".join(registry),
            )
        if not cls.available(env):
            raise ConfigError(
                f"the {prefer!r} launcher is not available on this machine",
                subject="launcher",
                remedy="Available here: " + (", ".join(available_launchers(env))
                                             or "none"),
            )
        return cls()

    if os.name == "nt":
        return registry["windows"]()
    if registry["systemd"].available(env):
        return registry["systemd"]()
    return registry["posix"]()


def launcher_for(handle: LaunchHandle) -> JobLauncher:
    """Reconstruct the launcher that started a recorded job.

    By name, from the handle - not by re-running the selection. A job launched under
    systemd must be cancelled through systemd even if the user manager has since become
    unavailable and selection would now choose the POSIX fallback.
    """
    cls = _registry().get(handle.backend)
    if cls is None:
        raise ConfigError(
            f"job was launched with an unknown launcher {handle.backend!r}",
            subject="launcher.backend",
            remedy="This job may have been launched by a different hydromate version.",
        )
    return cls()


# -------------------------------------------------------------------------- helpers


def runner_argv(job_dir: Path | str) -> list[str]:
    """The command a launcher starts: ``hydromate execute <job dir>``.

    Falls back to ``-m hydromate`` when the console script is not on ``PATH``, which is
    the normal situation inside a systemd unit whose environment came from a solver
    setup script rather than from the user's shell.
    """
    import shutil
    import sys
    exe = shutil.which("hydromate")
    if exe:
        return [exe, "execute", str(job_dir)]
    return [sys.executable, "-m", "hydromate", "execute", str(job_dir)]


def write_env_file(path: Path | str, env: dict[str, str]) -> Path:
    """Write a systemd-style ``KEY=value`` environment file.

    Line-based, so a value containing a newline cannot be represented. Those are dropped
    **and logged** rather than silently mangled - a truncated ``PATH`` produces a job
    that fails much later with an inexplicable "command not found".
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines, skipped = [], []
    for key, value in sorted(env.items()):
        text = str(value)
        if "\n" in text or "\r" in text or "\0" in text:
            skipped.append(key)
            continue
        lines.append(f"{key}={text}")
    if skipped:
        log.warning("dropped %d environment variable(s) whose value spans lines: %s",
                    len(skipped), ", ".join(skipped))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)      # a captured environment can carry credentials
    except OSError:              # pragma: no cover
        pass
    return target
