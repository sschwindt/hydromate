"""``job.lock``: one writer at a time, and a way back from a crash.

Two QGIS windows, or a CLI and QGIS, can act on the same job at once (plan §5). The lock
gives ``status.json`` exactly one writer. What makes it more than a file is what happens
when the holder dies: a lock nobody can break is worse than no lock, because a job that
crashed at 3 a.m. could then never be resubmitted.

So the record carries enough to judge staleness without guessing, and the checks run in
order of confidence:

1. **A different host** is never broken automatically. On a shared job root the PID means
   nothing here, so the honest answer is "cannot tell", and ``--force`` is a human
   decision.
2. **A different boot id** is stale: the machine restarted, so every PID it recorded is
   meaningless. This is also the ``wsl --shutdown`` case.
3. **A dead PID** is stale.
4. **A live PID with a different start time** is stale - PID reuse. Without this check a
   long-running job's recycled PID makes an unrelated process look like the lock holder,
   and the lock is never broken.

Readers never lock (plan §5); they read the last complete ``status.json``.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from axqua.core.errors import ConfigError
from axqua.jobs import procs
from axqua.jobs.model import utc_now
from axqua.jobs.paths import JobDir

__all__ = ["JobLock", "LockRecord", "LockedError"]


class LockedError(ConfigError):
    """Someone else holds the lock, and it is not stale."""

    code = "axqua.job.locked"


@dataclass
class LockRecord:
    pid: int
    started: str
    purpose: str = "execute"
    host: str = ""
    boot_id: str | None = None
    proc_started: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "started": self.started, "purpose": self.purpose,
                "host": self.host, "boot_id": self.boot_id,
                "proc_started": self.proc_started, **self.extra}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LockRecord":
        known = {"pid", "started", "purpose", "host", "boot_id", "proc_started"}
        return cls(
            pid=int(data.get("pid", 0) or 0),
            started=str(data.get("started") or ""),
            purpose=str(data.get("purpose") or "execute"),
            host=str(data.get("host") or ""),
            boot_id=data.get("boot_id"),
            proc_started=data.get("proc_started"),
            extra={k: v for k, v in data.items() if k not in known},
        )

    @classmethod
    def for_self(cls, purpose: str) -> "LockRecord":
        return cls(pid=os.getpid(), started=utc_now(), purpose=purpose,
                   host=procs.host_id(), boot_id=procs.boot_id(),
                   proc_started=procs.self_start_time())


class JobLock:
    """An advisory lock on one job directory."""

    def __init__(self, job_dir: JobDir | str | os.PathLike) -> None:
        self.job = job_dir if isinstance(job_dir, JobDir) else JobDir(job_dir)
        self.path: Path = self.job.lock
        self._held = False

    # -- inspection ---------------------------------------------------------------
    def owner(self) -> LockRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:                         # pragma: no cover
            return None
        try:
            return LockRecord.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            # A truncated lock file is itself evidence of a crash mid-write. Treat it as
            # a lock with no identifiable owner, which the staleness rules then break.
            return LockRecord(pid=0, started="", purpose="unknown")

    def is_stale(self) -> tuple[bool, str]:
        """``(stale, reason)``. A missing lock is not stale - it is absent."""
        record = self.owner()
        if record is None:
            return False, "no lock"
        if not record.pid:
            return True, "lock file is unreadable, so its owner cannot be identified"
        here = procs.host_id()
        if record.host and record.host != here:
            return False, (f"held on {record.host}, not {here}; axqua will not judge "
                           "a process on another machine")
        current_boot = procs.boot_id()
        if record.boot_id and current_boot and record.boot_id != current_boot:
            return True, "the machine has restarted since the lock was taken"
        if not procs.pid_alive(record.pid):
            return True, f"process {record.pid} is gone"
        now_started = procs.process_start_time(record.pid)
        if (record.proc_started is not None and now_started is not None
                and abs(now_started - record.proc_started) > 1e-6):
            return True, f"process {record.pid} was replaced (pid reuse)"
        return False, f"held by pid {record.pid}"

    # -- acquisition --------------------------------------------------------------
    def acquire(self, *, purpose: str = "execute", break_stale: bool = True,
                force: bool = False) -> "JobLock":
        record = LockRecord.for_self(purpose)
        payload = json.dumps(record.as_dict(), indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            stale, reason = self.is_stale()
            if not (force or (stale and break_stale)):
                held = self.owner()
                raise LockedError(
                    f"job {self.job.job_id} is locked ({reason})",
                    subject=str(self.path),
                    remedy="Wait for it to finish, or use --force if you are certain "
                           "the holder is gone.",
                    holder_pid=held.pid if held else None,
                    holder_host=held.host if held else None,
                ) from None
            # Breaking is a replace, not an unlink-then-create: an unlink leaves a
            # window in which a third process could take the lock we are about to claim.
            from axqua.jobs.store import write_json_atomic
            write_json_atomic(self.path, record.as_dict())
            self._held = True
            return self
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:               # pragma: no cover - already broken
            pass
        finally:
            self._held = False

    @property
    def held(self) -> bool:
        return self._held

    @contextmanager
    def holding(self, *, purpose: str = "execute", break_stale: bool = True,
                force: bool = False) -> Iterator["JobLock"]:
        self.acquire(purpose=purpose, break_stale=break_stale, force=force)
        try:
            yield self
        finally:
            self.release()

    def __enter__(self) -> "JobLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
