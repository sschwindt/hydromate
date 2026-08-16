"""Reconciling a job's recorded state with reality.

A detached job can die in ways it cannot report: the machine reboots, ``wsl --shutdown``
takes the distro with every job in it, an OOM killer removes the runner, someone runs
``kill -9``. In all of those the last thing written to ``status.json`` says ``RUNNING``,
and it will keep saying so forever.

A job **stuck in RUNNING is worse than a job marked FAILED**: the dashboard keeps polling
it, the user keeps waiting for it, and nothing ever prompts a resubmission. So every read
path that shows state to a human goes through here first, and one implementation covers
the POSIX fallback, systemd, WSL and Windows because they all reduce to the same
question - is the recorded process still the process we launched?

The one thing this must not do is guess about **another machine**. On a shared job root a
PID from another host says nothing here, so such a job is reported unchanged.
"""

from __future__ import annotations

import logging
import os

from hydromate.core.errors import ErrorRecord
from hydromate.jobs import procs
from hydromate.jobs.lock import JobLock
from hydromate.jobs.model import JobState, JobStatus
from hydromate.jobs.paths import JobDir
from hydromate.jobs.store import read_status, write_status

log = logging.getLogger("hydromate.jobs.reaper")

__all__ = ["reconcile"]


def reconcile(job_dir: JobDir | str | os.PathLike, *, write: bool = True
              ) -> JobStatus | None:
    """Return the job's true state, correcting ``status.json`` when it is wrong.

    *write* exists for read-only callers (a listing over a mounted archive) that still
    want the corrected answer in memory.
    """
    jd = job_dir if isinstance(job_dir, JobDir) else JobDir(job_dir)
    try:
        status = read_status(jd)
    except Exception as exc:                       # noqa: BLE001 - never take a list down
        log.debug("cannot read status of %s: %s", jd.root, exc)
        return None
    if status is None or status.state.is_terminal:
        return status

    handle = status.launcher or {}
    if not handle:
        # Nothing was ever launched. QUEUED is honest; so is a CANCEL_REQUESTED that
        # never got as far as a process.
        if status.state is JobState.CANCEL_REQUESTED:
            return _settle(jd, status, JobState.CANCELLED,
                           "cancelled before the job was launched", write=write)
        return status

    if handle.get("host") and handle["host"] != procs.host_id():
        return status                              # another machine: not ours to judge

    recorded_boot = handle.get("boot_id")
    current_boot = procs.boot_id()
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return _settle(jd, status, _resolution(jd),
                       "the machine restarted while the job was running", write=write)

    if _still_running(handle):
        return status

    # The process is gone but the job never wrote a terminal state, so it died without
    # getting the chance. Whether that reads as CANCELLED or FAILED is decided by
    # whether a cancellation had been asked for - which is exactly what cancel.request
    # records.
    return _settle(jd, status, _resolution(jd),
                   f"the runner process ({handle.get('pid')}) is gone and the job never "
                   "reported a result", write=write)


def _still_running(handle: dict) -> bool:
    """Ask the launcher that started it, falling back to the recorded PID."""
    try:
        from hydromate.jobs.launcher import LaunchHandle, LaunchState, launcher_for
        parsed = LaunchHandle.from_dict(handle)
        state = launcher_for(parsed).status(parsed)
        if state is LaunchState.RUNNING:
            return True
        if state is LaunchState.UNKNOWN:
            # "Cannot tell" must not be read as "dead" - a systemd unit on a machine
            # whose user manager is momentarily unreachable is still probably running.
            return True
        return False
    except Exception as exc:                       # noqa: BLE001
        log.debug("launcher status unavailable, falling back to pid: %s", exc)
    pid = handle.get("pid")
    if not pid:
        return False
    if not procs.pid_alive(pid):
        return False
    recorded = handle.get("proc_started")
    now = procs.process_start_time(pid)
    if recorded is not None and now is not None and abs(now - recorded) > 1e-6:
        return False                               # pid reuse: a different process
    return True


def _resolution(jd: JobDir) -> JobState:
    return JobState.CANCELLED if jd.cancel_request.exists() else JobState.FAILED


def _settle(jd: JobDir, status: JobStatus, target: JobState, reason: str, *,
            write: bool) -> JobStatus:
    if target is JobState.CANCELLED and status.state is JobState.RUNNING:
        # RUNNING cannot go straight to CANCELLED; pass through the request state so the
        # history reads truthfully rather than skipping a step.
        status.transition(JobState.CANCEL_REQUESTED)
    status.transition(target)
    if target is JobState.FAILED and status.error is None:
        status.error = ErrorRecord(
            code="hydromate.job.abandoned",
            message=reason,
            remedy="Check runner.log for how far it got, then resubmit.",
            details={"detected_by": "reconcile"},
        ).as_dict()
    log.info("job %s reconciled to %s: %s", status.job_id, target, reason)
    if write:
        try:
            lock = JobLock(jd)
            stale, _ = lock.is_stale()
            with lock.holding(purpose="reconcile", break_stale=stale):
                write_status(jd, status)
        except Exception as exc:                   # noqa: BLE001
            # Reporting the corrected state matters more than persisting it; a read-only
            # or contended directory must not turn a listing into an error.
            log.debug("could not persist reconciled status for %s: %s", jd.root, exc)
    return status
