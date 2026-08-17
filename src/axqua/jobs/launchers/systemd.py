"""systemd user services: the preferred Linux launcher.

A transient user unit gives, for free, all four things plan §8 asks for: the solver
survives the shell that started it, its state can be queried, its **entire process tree**
can be terminated as a unit, and its logs are retrievable. MPI ranks belong to the unit's
cgroup automatically, which is what makes cancellation reliable rather than best-effort -
a cgroup kill cannot miss a descendant the way signalling a process group can if a child
deliberately changes its own group.

Three details are load-bearing:

``--collect``
    The unit name is deterministic (``axqua-<job_id>.service``), which is what lets a
    later process find it. Without ``--collect`` a *failed* unit lingers in that name and
    the same job could never be resubmitted.

``EnvironmentFile``
    Rather than N x ``--setenv``. A captured OpenFOAM environment is large enough to
    exceed ``ARG_MAX``, and the failure is an opaque "Argument list too long" at submit
    time.

``KillMode=control-group``
    The default already kills the cgroup, but stating it makes the guarantee explicit
    rather than inherited.

``available()`` deliberately checks three things. A bare SSH session without lingering
has ``systemd-run`` on ``PATH`` and no user manager at all, so testing for the binary
alone is a false positive that produces a job which fails the moment it is submitted.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from axqua.core.errors import EnvironmentError as AxquaEnvironmentError
from axqua.jobs.launcher import LaunchHandle, LaunchState, write_env_file

log = logging.getLogger("axqua.jobs.launchers.systemd")

UNIT_PREFIX = "axqua-"


def unit_name(job_id: str) -> str:
    return f"{UNIT_PREFIX}{job_id}.service"


class SystemdUserLauncher:
    """``systemd-run --user`` transient units."""

    name = "systemd"

    @classmethod
    def available(cls, env: Any = None) -> bool:
        if os.name == "nt":
            return False
        if not shutil.which("systemd-run") or not shutil.which("systemctl"):
            return False
        # A user manager needs a runtime dir; without one `systemctl --user` cannot even
        # connect, which is the bare-SSH case.
        if not os.environ.get("XDG_RUNTIME_DIR"):
            return False
        try:
            proc = subprocess.run(["systemctl", "--user", "is-system-running"],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        # "degraded" is a perfectly usable manager (some unrelated unit failed), so the
        # test is whether it answered at all, not whether it is happy.
        return bool(proc.stdout.strip()) and "offline" not in proc.stdout

    def submit(self, job_dir: Any, argv: list[str], *, env: dict[str, str],
               cwd: Path | str) -> LaunchHandle:
        root = Path(getattr(job_dir, "root", job_dir))
        job_id = root.name
        unit = unit_name(job_id)
        env_file = write_env_file(root / "launch.env", env)
        stdout_path = root / "solver" / "stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)

        command = [
            "systemd-run", "--user", f"--unit={unit}", "--collect",
            f"--description=axqua job {job_id}",
            f"--property=WorkingDirectory={cwd}",
            f"--property=EnvironmentFile={env_file}",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=30",
            f"--property=StandardOutput=append:{stdout_path}",
            f"--property=StandardError=append:{stdout_path}",
            "--",
            *argv,
        ]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AxquaEnvironmentError(
                f"systemd-run could not start the job: {exc}",
                subject="launcher",
                remedy="Try --launcher posix, which needs no user manager.",
                argv=command,
            ) from exc
        if proc.returncode != 0:
            raise AxquaEnvironmentError(
                f"systemd-run failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}",
                subject="launcher",
                remedy="Try --launcher posix, which needs no user manager.",
                return_code=proc.returncode,
            )
        log.info("launched unit %s", unit)
        return LaunchHandle(backend=self.name, unit=unit, pid=self._main_pid(unit),
                            log_path=str(stdout_path))

    # -- inspection ---------------------------------------------------------------
    def _show(self, unit: str, *properties: str) -> dict[str, str]:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show", unit,
                 "--property=" + ",".join(properties)],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("systemctl show failed: %s", exc)
            return {}
        out = {}
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            if key:
                out[key.strip()] = value.strip()
        return out

    def _main_pid(self, unit: str) -> int | None:
        raw = self._show(unit, "MainPID").get("MainPID")
        try:
            pid = int(raw or 0)
        except ValueError:
            return None
        return pid or None

    def status(self, handle: LaunchHandle) -> LaunchState:
        if not handle.unit:
            return LaunchState.UNKNOWN
        info = self._show(handle.unit, "ActiveState", "SubState", "LoadState")
        if not info:
            # The manager did not answer. "Cannot tell" - not "dead"; see LaunchState.
            return LaunchState.UNKNOWN
        active = info.get("ActiveState", "")
        if active in ("active", "activating", "deactivating", "reloading"):
            return LaunchState.RUNNING
        if active in ("inactive", "failed"):
            return LaunchState.GONE
        if info.get("LoadState") == "not-found":
            # --collect already reaped it, which for a transient unit means finished.
            return LaunchState.GONE
        return LaunchState.UNKNOWN

    def cancel(self, handle: LaunchHandle, *, grace: float = 20.0) -> bool:
        """Stop the unit - which stops the whole cgroup, MPI ranks included."""
        if not handle.unit:
            return False
        try:
            proc = subprocess.run(["systemctl", "--user", "stop", handle.unit],
                                  capture_output=True, text=True,
                                  timeout=max(30.0, grace + 10.0))
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not stop %s: %s", handle.unit, exc)
            return False
        if proc.returncode != 0:
            log.warning("systemctl stop %s returned %s: %s", handle.unit,
                        proc.returncode, (proc.stderr or "").strip())
        return self.status(handle) is not LaunchState.RUNNING

    def logs(self, handle: LaunchHandle) -> Path | None:
        """The unit's captured stdout.

        A file rather than ``journalctl``: the plugin needs a path it can tail, and the
        unit is configured to append to one, so the journal is the fallback for a human
        rather than the interface.
        """
        return Path(handle.log_path) if handle.log_path else None
