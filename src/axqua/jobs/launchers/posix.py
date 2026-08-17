"""The POSIX fallback: a new session, and a process group to kill.

Used wherever systemd's user manager is not available - a container, a machine without
lingering enabled, macOS. Less capable than a systemd unit (no supervision, no restart,
no journal) but sufficient for the one thing that matters: the job survives the shell
that started it, and it can still be terminated as a whole.

``start_new_session=True`` is the entire trick. It calls ``setsid``, which makes the
child a **session and process-group leader**, so its own children - TELEMAC's launcher,
``mpirun``, the ranks - inherit that process group. ``killpg`` then reaches all of them.
Without it the child joins the submitter's process group, and cancelling would either
miss the ranks or kill the terminal that launched them.

The parent never waits. It returns immediately, exits, and the child is reparented to
init; that is what "detached" means here (plan §8).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from axqua.core.errors import EnvironmentError as AxquaEnvironmentError
from axqua.jobs import procs
from axqua.jobs.launcher import LaunchHandle, LaunchState

log = logging.getLogger("axqua.jobs.launchers.posix")


class PosixDetachedLauncher:
    """``Popen(..., start_new_session=True)`` plus a recorded process group."""

    name = "posix"

    @classmethod
    def available(cls, env: Any = None) -> bool:
        return os.name != "nt"

    def submit(self, job_dir: Any, argv: list[str], *, env: dict[str, str],
               cwd: Path | str) -> LaunchHandle:
        root = Path(getattr(job_dir, "root", job_dir))
        stdout_path = root / "solver" / "stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        handle_out = open(stdout_path, "ab")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                # No terminal: a detached job that inherits a tty will be killed by
                # SIGHUP when that terminal closes, which is the exact failure mode this
                # launcher exists to prevent.
                stdin=subprocess.DEVNULL,
                stdout=handle_out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise AxquaEnvironmentError(
                f"could not start the job runner: {exc}",
                subject="launcher",
                remedy="Check that the axqua executable is on PATH for the "
                       "captured solver environment.",
                argv=argv,
                errno=getattr(exc, "errno", None),
            ) from exc
        finally:
            handle_out.close()

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:                      # pragma: no cover - exited immediately
            pgid = proc.pid
        log.info("launched pid %s (pgid %s) detached", proc.pid, pgid)
        return LaunchHandle(backend=self.name, pid=proc.pid, pgid=pgid,
                            proc_started=procs.process_start_time(proc.pid),
                            log_path=str(stdout_path))

    def status(self, handle: LaunchHandle) -> LaunchState:
        if not handle.pid:
            return LaunchState.UNKNOWN
        if not procs.pid_alive(handle.pid):
            return LaunchState.GONE
        now = procs.process_start_time(handle.pid)
        if (handle.proc_started is not None and now is not None
                and abs(now - handle.proc_started) > 1e-6):
            # The PID is live but belongs to something else - the job is gone and its
            # number was recycled.
            return LaunchState.GONE
        return LaunchState.RUNNING

    def cancel(self, handle: LaunchHandle, *, grace: float = 20.0) -> bool:
        """SIGTERM the group, then SIGKILL what is left.

        The group, never the pid: the ranks are grandchildren, and signalling the leader
        alone leaves orphan ``mpirun`` processes running at full tilt (plan §9).
        """
        pgid = handle.pgid or handle.pid
        if not pgid:
            return False
        if not self._signal(pgid, signal.SIGTERM):
            return False
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            if self.status(handle) is LaunchState.GONE:
                return True
            time.sleep(0.25)
        log.warning("pgid %s did not stop within %.0fs; sending SIGKILL", pgid, grace)
        self._signal(pgid, signal.SIGKILL)
        # A short settle so a caller that immediately re-reads the state sees the truth.
        time.sleep(0.25)
        return self.status(handle) is LaunchState.GONE

    @staticmethod
    def _signal(pgid: int, sig: int) -> bool:
        try:
            os.killpg(int(pgid), sig)
        except ProcessLookupError:
            return True                     # already gone: the desired end state
        except PermissionError:
            log.error("not permitted to signal process group %s", pgid)
            return False
        except OSError as exc:              # pragma: no cover
            log.error("could not signal process group %s: %s", pgid, exc)
            return False
        return True

    def logs(self, handle: LaunchHandle) -> Path | None:
        return Path(handle.log_path) if handle.log_path else None
