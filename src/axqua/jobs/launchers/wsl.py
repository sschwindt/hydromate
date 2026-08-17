"""WSL: detach *inside* the distro, and act on it only through ``wsl.exe``.

Foundation OpenFOAM has no native Windows build, so on Windows it is reached through WSL
(plan §6). That makes the launcher a composition rather than a new mechanism: the job is
detached with ``setsid``/``nohup`` inside the Linux distro exactly as
:class:`PosixDetachedLauncher` would, and the Windows side records the **Linux** PID and
runs every later query or signal back through ``wsl.exe``.

Two caveats are documented rather than pretended away, because both will be met in
practice:

**Killing ``wsl.exe`` does not kill the Linux tree.** It is a client that talks to the
distro's init, not the parent of what it started. Cancellation must therefore run *inside*
the distro and act on the Linux process group - which is what :meth:`cancel` does. A
launcher that terminated the ``wsl.exe`` process would report success and leave the solver
running.

**The distro's lifetime is not ours.** ``wsl --shutdown``, a Windows restart, or WSL2
idling the VM out will terminate running jobs regardless of anything axqua does. Such
jobs must recover to ``FAILED`` rather than a stuck ``RUNNING`` - which they do, through
the same stale-process detection the POSIX fallback uses, because the recorded PID simply
stops existing. One implementation, not two.

**Never executed against a real WSL install.** Command construction is unit-tested; the
rest is not.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from axqua.core.environment import EnvironmentKind
from axqua.core.errors import EnvironmentError as AxquaEnvironmentError
from axqua.jobs.launcher import LaunchHandle, LaunchState

log = logging.getLogger("axqua.jobs.launchers.wsl")


class WslLauncher:
    """Detach inside the distro; monitor and cancel across the boundary."""

    name = "wsl"

    def __init__(self, distro: str | None = None) -> None:
        self.distro = distro

    @classmethod
    def available(cls, env: Any = None) -> bool:
        if not shutil.which("wsl.exe") and not shutil.which("wsl"):
            return False
        if env is not None:
            return getattr(env, "kind", None) is EnvironmentKind.WSL
        return True

    # -- command construction (the tested part) -----------------------------------
    def _prefix(self, distro: str | None) -> list[str]:
        exe = shutil.which("wsl.exe") or "wsl.exe"
        prefix = [exe]
        if distro:
            prefix += ["-d", distro]
        return prefix + ["--"]

    def _bash(self, distro: str | None, payload: str) -> list[str]:
        return self._prefix(distro) + ["bash", "-lc", payload]

    def launch_payload(self, argv: list[str], cwd: str, log_path: str) -> str:
        """The shell one-liner that detaches the runner inside the distro.

        ``setsid`` gives the job its own session (so it is not killed when the ``wsl.exe``
        client goes away), ``nohup`` detaches it from any hangup, stdin comes from
        ``/dev/null``, and the last ``echo`` returns the Linux PID - which is the only
        thing the Windows side can use afterwards.
        """
        command = " ".join(shlex.quote(a) for a in argv)
        return (f"cd {shlex.quote(cwd)} && "
                f"setsid nohup {command} >> {shlex.quote(log_path)} 2>&1 < /dev/null & "
                f"echo $!")

    # -- submit -------------------------------------------------------------------
    def submit(self, job_dir: Any, argv: list[str], *, env: dict[str, str],
               cwd: Path | str) -> LaunchHandle:
        root = Path(getattr(job_dir, "root", job_dir))
        distro = self.distro or env.get("AXQUA_WSL_DISTRO") or None
        # Every path crosses the boundary, so every path is translated - once, here.
        from axqua.core.environment import windows_to_wsl
        linux_cwd = windows_to_wsl(str(cwd))
        linux_log = windows_to_wsl(str(root / "solver" / "stdout.log"))
        (root / "solver").mkdir(parents=True, exist_ok=True)

        payload = self.launch_payload(argv, linux_cwd, linux_log)
        try:
            proc = subprocess.run(self._bash(distro, payload), capture_output=True,
                                  text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AxquaEnvironmentError(
                f"could not reach the WSL distro: {exc}",
                subject="environment.distro",
                remedy="Check 'wsl -l -v' and that the distro name in the profile "
                       "matches.",
                distro=distro,
            ) from exc
        if proc.returncode != 0:
            raise AxquaEnvironmentError(
                f"wsl.exe failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}",
                subject="environment.distro",
                return_code=proc.returncode,
            )
        pid = _last_int(proc.stdout)
        log.info("launched linux pid %s in distro %s", pid, distro or "(default)")
        return LaunchHandle(backend=self.name, pid=pid, pgid=pid, distro=distro,
                            log_path=str(root / "solver" / "stdout.log"))

    # -- inspection ---------------------------------------------------------------
    def _inside(self, handle: LaunchHandle, payload: str, *, timeout: float = 30.0):
        try:
            return subprocess.run(self._bash(handle.distro, payload),
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("wsl command failed: %s", exc)
            return None

    def status(self, handle: LaunchHandle) -> LaunchState:
        if not handle.pid:
            return LaunchState.UNKNOWN
        proc = self._inside(handle, f"kill -0 {int(handle.pid)} 2>/dev/null && echo up")
        if proc is None:
            # The distro may simply be starting up. Not an answer, so not "gone".
            return LaunchState.UNKNOWN
        return LaunchState.RUNNING if "up" in proc.stdout else LaunchState.GONE

    def cancel(self, handle: LaunchHandle, *, grace: float = 20.0) -> bool:
        """Signal the Linux **process group**, from inside the distro.

        The negative pid is the process group, which is what ``setsid`` made the runner
        the leader of - so this reaches ``mpirun`` and its ranks.
        """
        pgid = handle.pgid or handle.pid
        if not pgid:
            return False
        self._inside(handle, f"kill -TERM -{int(pgid)} 2>/dev/null || true")
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            if self.status(handle) is LaunchState.GONE:
                return True
            time.sleep(0.5)
        log.warning("linux pgid %s did not stop within %.0fs; sending SIGKILL", pgid,
                    grace)
        self._inside(handle, f"kill -KILL -{int(pgid)} 2>/dev/null || true")
        time.sleep(0.5)
        return self.status(handle) is not LaunchState.RUNNING

    def logs(self, handle: LaunchHandle) -> Path | None:
        return Path(handle.log_path) if handle.log_path else None


def _last_int(text: str) -> int | None:
    """The PID from the launch output.

    The *last* integer line, not the first: a login shell may print a banner, and WSL
    itself sometimes emits a notice before the command's own output.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None
