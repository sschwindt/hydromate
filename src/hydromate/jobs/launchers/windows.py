"""Windows: detached processes inside a named Job Object.

Windows has no process groups in the POSIX sense, so ``killpg`` has no equivalent and the
naive ``proc.terminate()`` kills only the process you named - leaving every MPI rank it
started running at full tilt. A **Job Object** is the actual equivalent: a kernel object
that owns a set of processes and can terminate all of them at once.

Two flags decide whether this works, and the plan's sketch gets one of them subtly wrong:

``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` must be **cleared**
    Otherwise the job dies when the last handle to the object closes - i.e. when
    ``hydromate submit`` exits, which is the opposite of detached.

The object must be **named**
    An unnamed Job Object is reachable only through the handle that created it, and that
    handle dies with the submitting process. A later ``hydromate cancel``, in a different
    process, then has nothing to open - so cancellation would be impossible, which is
    exactly the failure the Job Object was introduced to prevent. Hence
    ``Local\\hydromate-<job_id>`` plus ``OpenJobObjectW`` at cancel time.

**Never executed against a real Windows install.** The argv and flag construction are
unit-tested because they are host-independent; the ``kernel32`` calls are not, and this
module says so rather than implying a coverage it does not have.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from hydromate.core.errors import EnvironmentError as HydromateEnvironmentError
from hydromate.jobs import procs
from hydromate.jobs.launcher import LaunchHandle, LaunchState

log = logging.getLogger("hydromate.jobs.launchers.windows")

# CreateProcess flags. DETACHED_PROCESS gives the child no console at all, so closing
# the launching terminal cannot deliver CTRL_CLOSE_EVENT to the solver.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# SetInformationJobObject / JOBOBJECT_EXTENDED_LIMIT_INFORMATION
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_ALL_ACCESS = 0x1F001F


def job_object_name(job_id: str) -> str:
    """Session-local, so two users on one machine cannot collide."""
    return f"Local\\hydromate-{job_id}"


class WindowsJobObjectLauncher:
    """Detached ``CreateProcess`` assigned to a named Job Object."""

    name = "windows"

    @classmethod
    def available(cls, env: Any = None) -> bool:
        return os.name == "nt"

    # -- submit -------------------------------------------------------------------
    def submit(self, job_dir: Any, argv: list[str], *, env: dict[str, str],
               cwd: Path | str) -> LaunchHandle:
        root = Path(getattr(job_dir, "root", job_dir))
        stdout_path = root / "solver" / "stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        name = job_object_name(root.name)

        job = self._create_job_object(name)
        creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        handle_out = open(stdout_path, "ab")
        try:
            proc = self._spawn(argv, cwd, env, creationflags, handle_out)
            assigned = self._assign(job, proc)
            if not assigned:
                # The submitter is itself inside a job that forbids nesting (some CI
                # runners and terminals do this). Retry breaking away, and if that also
                # fails, run detached without a job object and say so - a job that runs
                # and is awkward to cancel beats a job that will not start.
                proc.kill()
                proc = self._spawn(argv, cwd, env,
                                   creationflags | CREATE_BREAKAWAY_FROM_JOB, handle_out)
                assigned = self._assign(job, proc)
        finally:
            handle_out.close()

        if not assigned:
            log.warning("could not assign the job to a Job Object; cancellation will "
                        "fall back to taskkill /T, which is less reliable")
            name = None
        log.info("launched pid %s detached%s", proc.pid,
                 f" in job object {name}" if name else "")
        return LaunchHandle(backend=self.name, pid=proc.pid, job_object=name,
                            proc_started=procs.process_start_time(proc.pid),
                            log_path=str(stdout_path))

    @staticmethod
    def _spawn(argv, cwd, env, creationflags, stdout):
        try:
            return subprocess.Popen(
                argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=subprocess.STDOUT, close_fds=True,
                creationflags=creationflags)
        except OSError as exc:
            raise HydromateEnvironmentError(
                f"could not start the job runner: {exc}",
                subject="launcher",
                remedy="Check that hydromate is on PATH inside the solver environment.",
                argv=list(argv),
            ) from exc

    # -- kernel32 -----------------------------------------------------------------
    @staticmethod
    def _kernel32():  # pragma: no cover - Windows only
        import ctypes
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def _create_job_object(self, name: str):  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = self._kernel32()
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, name)
        if not job:
            log.warning("CreateJobObject(%s) failed (%s)", name,
                        ctypes.get_last_error())
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # Explicitly clear the kill-on-close bit: the whole point is that the tree
        # outlives the process that created the object.
        info.BasicLimitInformation.LimitFlags &= ~JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                         ctypes.byref(info), ctypes.sizeof(info))
        return job

    def _assign(self, job, proc) -> bool:  # pragma: no cover - Windows only
        if not job:
            return False
        from ctypes import wintypes
        kernel32 = self._kernel32()
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1F0FFF, False, proc.pid)   # PROCESS_ALL_ACCESS
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, handle))
        finally:
            kernel32.CloseHandle(handle)

    # -- inspection ---------------------------------------------------------------
    def status(self, handle: LaunchHandle) -> LaunchState:
        if not handle.pid:
            return LaunchState.UNKNOWN
        if not procs.pid_alive(handle.pid):
            return LaunchState.GONE
        now = procs.process_start_time(handle.pid)
        if (handle.proc_started is not None and now is not None
                and abs(now - handle.proc_started) > 1e-6):
            return LaunchState.GONE
        return LaunchState.RUNNING

    def cancel(self, handle: LaunchHandle, *, grace: float = 20.0) -> bool:
        """``TerminateJobObject`` on the named object, with ``taskkill /T`` as fallback."""
        if handle.job_object and self._terminate_job(handle.job_object):
            return self.status(handle) is not LaunchState.RUNNING
        if not handle.pid:
            return False
        log.info("falling back to taskkill for pid %s", handle.pid)
        try:
            # /T is the tree, /F is force. Weaker than a Job Object because it walks
            # parent-child links, which a re-parented rank has already lost.
            subprocess.run(["taskkill", "/PID", str(handle.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=max(30.0, grace))
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("taskkill failed: %s", exc)
            return False
        return self.status(handle) is not LaunchState.RUNNING

    def _terminate_job(self, name: str) -> bool:  # pragma: no cover - Windows only
        try:
            from ctypes import wintypes
            kernel32 = self._kernel32()
            kernel32.OpenJobObjectW.restype = wintypes.HANDLE
            job = kernel32.OpenJobObjectW(JOB_OBJECT_ALL_ACCESS, False, name)
            if not job:
                return False
            try:
                return bool(kernel32.TerminateJobObject(job, 1))
            finally:
                kernel32.CloseHandle(job)
        except Exception as exc:  # noqa: BLE001
            log.debug("TerminateJobObject(%s) failed: %s", name, exc)
            return False

    def logs(self, handle: LaunchHandle) -> Path | None:
        return Path(handle.log_path) if handle.log_path else None
