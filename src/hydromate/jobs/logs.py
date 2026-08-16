"""Job logging: bounded, and split by who is talking.

Two separate streams, because they answer different questions and want different
policies (plan §23):

``runner.log``
    hydromate's own narration - state transitions, the commands it issued, exit codes,
    ``log_step`` timings. Small, and the thing to read first when a job fails.
``solver/<name>.log``
    The solver talking. TELEMAC prints a block per time step and interFoam at ~1e-3 s
    steps over hours produces hundreds of thousands of lines; a multi-day run will
    otherwise fill the scratch volume and take itself down.

Both rotate, and **rotation keeps the last parts**. That is worth stating because the
naive alternative - stop writing at N bytes - keeps the *first*, and when a run fails
after two days the beginning is precisely the part nobody needs.
``logging.handlers.RotatingFileHandler`` already behaves correctly here: the live file is
always the newest and ``.1`` the most recent rollover.

The disk guard is here too. "Insufficient disk space where detectable" (plan §24) is six
lines and turns a mid-run death with a corrupt half-written result into a refusal at
submit time carrying a remedy.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hydromate.core.errors import EnvironmentError as HydromateEnvironmentError

#: 32 MiB x 5 for hydromate's narration: generous for state transitions, bounded enough
#: that a runaway warning loop cannot fill a volume.
MAX_BYTES = 32 * 1024 * 1024
BACKUP_COUNT = 5

#: The solver's own listing is the bulky one, and its tail is the diagnostic.
SOLVER_MAX_BYTES = 128 * 1024 * 1024
SOLVER_BACKUPS = 3

#: A floor, not a forecast. A real OpenFOAM run wants far more; this catches the volume
#: that is already full.
MIN_FREE_BYTES = 5 * 1024 ** 3

log = logging.getLogger("hydromate.jobs.logs")

__all__ = ["RotatingTextSink", "free_bytes", "require_free_space", "runner_log"]


@contextmanager
def runner_log(path: str | os.PathLike, *, level: int = logging.DEBUG,
               console: bool = False, max_bytes: int = MAX_BYTES,
               backups: int = BACKUP_COUNT) -> Iterator[Path]:
    """Route the ``hydromate`` logger into a rotating ``runner.log`` for the duration.

    Scoped rather than global because a job runs inside a process that may also be doing
    something else (``hydromate execute`` in a terminal), and the handler must come off
    cleanly afterwards.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger("hydromate")
    previous = root.level
    root.addHandler(handler)
    if previous > level or previous == logging.NOTSET:
        root.setLevel(level)
    stream: logging.Handler | None = None
    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.INFO)
        root.addHandler(stream)
    try:
        yield target
    finally:
        root.removeHandler(handler)
        handler.close()
        if stream is not None:
            root.removeHandler(stream)
            stream.close()
        root.setLevel(previous)


class RotatingTextSink:
    """Append raw solver output to a size-bounded file.

    Built on ``RotatingFileHandler`` rather than hand-rolled, so the rollover semantics
    are the standard library's and the *last* parts survive. Line-buffered writes: a job
    that is killed must still have its most recent output on disk.
    """

    def __init__(self, path: str | os.PathLike, *, max_bytes: int = SOLVER_MAX_BYTES,
                 backups: int = SOLVER_BACKUPS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger(f"hydromate.solver-output.{id(self):x}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False          # never leak the listing into runner.log
        self._logger.addHandler(self._handler)

    def write(self, line: str) -> None:
        self._logger.info("%s", str(line).rstrip("\n"))

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()

    def __enter__(self) -> "RotatingTextSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def free_bytes(path: str | os.PathLike) -> int | None:
    """Free space on the volume holding *path*, or ``None`` if it cannot be told."""
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:                              # pragma: no cover
        return None


def require_free_space(path: str | os.PathLike, minimum: int = MIN_FREE_BYTES) -> None:
    """Refuse to start when the volume is already too full to finish."""
    if minimum <= 0:
        return
    available = free_bytes(path)
    if available is None or available >= minimum:
        return
    raise HydromateEnvironmentError(
        f"only {available / 1024 ** 3:.1f} GiB free on the volume holding {path}, "
        f"and hydromate wants at least {minimum / 1024 ** 3:.1f} GiB",
        subject="resources.min_free_bytes",
        remedy="Free space, or point the job root at a larger volume "
               "(--job-root, $HYDROMATE_JOB_ROOT, or the profile's working_root).",
        free_bytes=available,
        required_bytes=minimum,
    )
