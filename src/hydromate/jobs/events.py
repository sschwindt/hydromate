"""Turning a stream of solver lines into job state.

hydromate's existing progress reporting is **console-shaped**: ``on_line`` callbacks that
draw a bar on a TTY. A detached job has no TTY and needs the same information as
*structured state* in ``status.json``, so that QGIS never has to parse a human-readable
log (plan §11, explicitly).

The wrong fix would be a second parser. ``SolverProgress`` already extracts the iteration
and the simulated time from TELEMAC's listing header, and ``OpenFoamProgress`` already
extracts the Courant number and the time step from interFoam's - both hard-won against
real output. So this module adds a **sink** those classes also emit to, and everything
composes:

* :class:`ConsoleSink` is today's behaviour, unchanged, for terminal runs;
* :class:`StatusFileSink` writes ``status.json``;
* :class:`TeeSink` does both, which is what ``hydromate execute`` wants - a debugging run
  should look exactly like the old scripts *and* leave a machine-readable trail.

:class:`StatusFileSink` is throttled. Without it a TELEMAC listing at a few headers a
second produces several ``fsync``-ed rewrites a second on a scratch volume, which is both
wasteful and a reliable way to make a network filesystem unhappy. State and step changes
are never throttled - they are what the dashboard is waiting for.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from hydromate.jobs.model import JobStatus, Progress, utc_now

log = logging.getLogger("hydromate.jobs.events")

#: Minimum seconds between throttled ``status.json`` writes.
STATUS_MIN_INTERVAL = 2.0

__all__ = ["ConsoleSink", "EventSink", "NullSink", "StatusFileSink", "TeeSink",
           "TextLogSink", "current_sink", "using_sink"]


@runtime_checkable
class EventSink(Protocol):
    """What a long-running operation reports.

    Five verbs, because five different things happen and collapsing them into one
    ``log(str)`` is exactly what forces a GUI to parse prose.
    """

    def line(self, text: str) -> None:
        """One raw line of solver output."""

    def progress(self, **fields: Any) -> None:
        """Typed progress fields for the current job kind."""

    def step(self, name: str, *, done: bool = False, seconds: float | None = None,
             failed: bool = False) -> None:
        """A named phase started, finished or failed."""

    def artifact(self, kind: str, path: Path | str) -> None:
        """A file worth recording in the job's artifact map."""

    def decision(self, question: str, answer: bool, source: str) -> None:
        """A question the job answered on the user's behalf, and why."""


class NullSink:
    """Discards everything. The default, so nothing has to check for ``None``."""

    def line(self, text: str) -> None:
        pass

    def progress(self, **fields: Any) -> None:
        pass

    def step(self, name: str, *, done: bool = False, seconds: float | None = None,
             failed: bool = False) -> None:
        pass

    def artifact(self, kind: str, path: Path | str) -> None:
        pass

    def decision(self, question: str, answer: bool, source: str) -> None:
        pass


class ConsoleSink(NullSink):
    """Print. This is what a terminal run has always done."""

    def __init__(self, *, echo: bool = True) -> None:
        self.echo = echo

    def line(self, text: str) -> None:
        if self.echo:
            print(text)

    def step(self, name: str, *, done: bool = False, seconds: float | None = None,
             failed: bool = False) -> None:
        if failed:
            print(f"FAILED {name}")
        elif done:
            print(f"done {name}" + (f" in {seconds:.2f}s" if seconds else ""))


class TextLogSink(NullSink):
    """Append raw lines to a rotating text file - the solver's own narration.

    Deliberately separate from ``runner.log`` (plan §23): hydromate's narration is state
    transitions and exit codes, while this is the solver talking. Mixing them makes both
    harder to read and makes the rotation policy wrong for at least one of them.
    """

    def __init__(self, writer: Any) -> None:
        self.writer = writer

    def line(self, text: str) -> None:
        self.writer.write(text)

    def step(self, name: str, *, done: bool = False, seconds: float | None = None,
             failed: bool = False) -> None:
        if failed:
            self.writer.write(f"--- FAILED {name}")
        elif done:
            self.writer.write(f"--- done {name}")
        else:
            self.writer.write(f"--- {name}")


class StatusFileSink(NullSink):
    """Fold events into ``status.json``.

    Holds the :class:`JobStatus` in memory and replaces the file; the executor owns the
    lock for the duration of the run, so there is exactly one writer.
    """

    def __init__(self, job_dir: Any, status: JobStatus, *,
                 min_interval: float = STATUS_MIN_INTERVAL) -> None:
        self.job_dir = job_dir
        self.status = status
        self.min_interval = float(min_interval)
        #: ``None`` means "never written", which is **not** the same as "written at time
        #: zero". Initialising this to 0.0 made the throttle compare against a fake past
        #: write, and on a freshly-booted machine - where ``time.monotonic()`` is still a
        #: small number - the very first progress update was therefore throttled away.
        #: The job then published no progress at all until something forced a write. On a
        #: long-uptime machine the clock is large enough that it never showed.
        self._last_write: float | None = None
        self._dirty = False

    # -- events -------------------------------------------------------------------
    def progress(self, **fields: Any) -> None:
        current = self.status.progress
        if isinstance(current, Progress):
            self.status.progress = current.merged(**fields)
        elif isinstance(current, dict):
            self.status.progress = {**current,
                                    **{k: v for k, v in fields.items() if v is not None}}
        else:
            self.status.progress = dict(fields)
        self._touch(force=False)

    def step(self, name: str, *, done: bool = False, seconds: float | None = None,
             failed: bool = False) -> None:
        # A phase change is exactly what a watching dashboard is waiting for, so it is
        # never throttled.
        self.status.phase = "" if done or failed else name
        self._touch(force=True)

    def artifact(self, kind: str, path: Path | str) -> None:
        self.status.artifacts[str(kind)] = str(path)
        self._touch(force=True)

    def decision(self, question: str, answer: bool, source: str) -> None:
        progress = self.status.progress
        if isinstance(progress, Progress) and hasattr(progress, "pending_question"):
            self.status.progress = progress.merged(pending_question=None)
        log.info("answered %r with %s (%s)", question, answer, source)
        self._touch(force=True)

    def pending(self, question: str | None) -> None:
        """Record a question the job is about to answer, so the dashboard can show it."""
        progress = self.status.progress
        if isinstance(progress, Progress) and hasattr(progress, "pending_question"):
            object.__setattr__(progress, "pending_question", question)
            self._touch(force=True)

    # -- writing ------------------------------------------------------------------
    def _touch(self, *, force: bool) -> None:
        self._dirty = True
        now = time.monotonic()
        # The first write is never throttled: a dashboard that shows nothing until the
        # second event looks like a job that has not started.
        if (not force and self._last_write is not None
                and (now - self._last_write) < self.min_interval):
            return
        self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        from hydromate.jobs.store import write_status
        self.status.updated = utc_now()
        try:
            write_status(self.job_dir, self.status)
        except Exception as exc:                    # noqa: BLE001
            # A status write that fails must not take the solver down with it. The job
            # itself is still running and its real output is unaffected.
            log.debug("could not write status.json: %s", exc)
            return
        self._last_write = time.monotonic()
        self._dirty = False

    def close(self) -> None:
        self.flush()


class TeeSink(NullSink):
    """Fan out to several sinks. A failing sink never stops the others."""

    def __init__(self, *sinks: Any) -> None:
        self.sinks = [s for s in sinks if s is not None]

    def _each(self, method: str, *args: Any, **kwargs: Any) -> None:
        for sink in self.sinks:
            fn = getattr(sink, method, None)
            if fn is None:
                continue
            try:
                fn(*args, **kwargs)
            except Exception as exc:                # noqa: BLE001
                log.debug("sink %r failed on %s: %s", sink, method, exc)

    def line(self, text: str) -> None:
        self._each("line", text)

    def progress(self, **fields: Any) -> None:
        self._each("progress", **fields)

    def step(self, name: str, *, done: bool = False, seconds: float | None = None,
             failed: bool = False) -> None:
        self._each("step", name, done=done, seconds=seconds, failed=failed)

    def artifact(self, kind: str, path: Path | str) -> None:
        self._each("artifact", kind, path)

    def decision(self, question: str, answer: bool, source: str) -> None:
        self._each("decision", question, answer, source)

    def flush(self) -> None:
        self._each("flush")

    def close(self) -> None:
        self._each("close")


#: The ambient sink, so ``log_step`` can report phases without every one of its ~40 call
#: sites growing a parameter. A ContextVar rather than a module global because a
#: convergence study runs nested steps and must not leak its sink to a sibling.
current_sink: ContextVar[Any] = ContextVar("hydromate_event_sink", default=NullSink())


@contextmanager
def using_sink(sink: Any) -> Iterator[Any]:
    token = current_sink.set(sink if sink is not None else NullSink())
    try:
        yield sink
    finally:
        current_sink.reset(token)


def emit_step(name: str, *, done: bool = False, seconds: float | None = None,
              failed: bool = False) -> None:
    """Report a phase to the ambient sink. Never raises."""
    try:
        current_sink.get().step(name, done=done, seconds=seconds, failed=failed)
    except Exception:                               # noqa: BLE001 - pragma: no cover
        pass
