"""Running CLI calls off the GUI thread.

``QgsTask`` is used here for exactly what plan §22 permits: short-lived QGIS-side
operations that are safe to terminate when QGIS exits. **Never** to own a solver - the
solver belongs to the detached job, which is the point of the whole architecture.

So the tasks in this module wrap things like "submit this job" (a subprocess call that
takes a second or two) or "list the jobs" (a directory scan on a possibly slow scratch
volume). If QGIS is closed halfway through one, nothing is lost: the job either was
submitted or was not, and the next dashboard refresh discovers the truth from disk.
"""

from __future__ import annotations

from typing import Any, Callable

from qgis.core import QgsApplication, QgsTask

from .runner_client import RunnerError


class RunnerTask(QgsTask):
    """Call axqua in the background and hand the result back on the main thread.

    Both callbacks run on the main thread (Qt delivers ``finished`` there), so they may
    touch widgets and the layer tree freely - which is precisely why the subprocess call
    has to happen in ``run`` and nothing else does.
    """

    def __init__(self, description: str, work: Callable[[], Any], *,
                 on_success: Callable[[Any], None] | None = None,
                 on_error: Callable[[Exception], None] | None = None) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self._work = work
        self._on_success = on_success
        self._on_error = on_error
        self._result: Any = None
        self._error: Exception | None = None

    def run(self) -> bool:
        try:
            self._result = self._work()
        except RunnerError as exc:
            self._error = exc
            return False
        except Exception as exc:  # noqa: BLE001
            # A background thread that raises would take QGIS's task manager with it, so
            # everything is caught and reported through the error callback instead.
            self._error = exc
            return False
        return True

    def finished(self, ok: bool) -> None:
        try:
            if ok and self._on_success is not None:
                self._on_success(self._result)
            elif not ok and self._on_error is not None:
                self._on_error(self._error or RuntimeError("the task was cancelled"))
        finally:
            _IN_FLIGHT.discard(self)

    @property
    def result(self) -> Any:
        return self._result

    @property
    def error(self) -> Exception | None:
        return self._error


#: In-flight tasks, held so Python does not collect them.
#:
#: This is not defensive programming, it is a real PyQGIS trap: ``addTask`` does not take
#: a Python reference, so a task whose only reference was a local variable is garbage
#: collected the moment the calling function returns - and the symptom is not a crash but
#: a callback that simply never fires. The dashboard stays empty, the tabs never appear,
#: and nothing anywhere reports an error. Entries are dropped again in ``finished``.
_IN_FLIGHT: "set[RunnerTask]" = set()


def run_async(description: str, work: Callable[[], Any], *,
              on_success: Callable[[Any], None] | None = None,
              on_error: Callable[[Exception], None] | None = None) -> RunnerTask:
    """Queue *work* on QGIS's task manager and return the task."""
    task = RunnerTask(description, work, on_success=on_success, on_error=on_error)
    _IN_FLIGHT.add(task)
    QgsApplication.taskManager().addTask(task)
    return task
