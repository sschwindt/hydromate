"""Progress reporting, log rotation and the disk guard.

The theme: a detached job must publish *structured* state, and it must do so without
either flooding the disk or ever letting reporting failures touch the solver.
"""

from __future__ import annotations

import logging

import pytest

from hydromate.jobs import events, logs
from hydromate.jobs.model import JobKind, JobSpec, JobStatus, SolverRunProgress
from hydromate.jobs.paths import JobDir


@pytest.fixture
def job(tmp_path) -> JobDir:
    return JobDir(tmp_path / "2026-08-14-demo-steady-a1b2c3").create()


@pytest.fixture
def status() -> JobStatus:
    spec = JobSpec(job_id="2026-08-14-demo-steady-a1b2c3", kind=JobKind.STEADY_RUN,
                   solver="telemac")
    return JobStatus.initial(spec)


# ---------------------------------------------------------------- the parsing seam


def test_the_telemac_parser_feeds_a_sink_without_a_second_parser():
    """``SolverProgress`` already extracts the iteration and simulated time from a real
    listing header; the sink is a second consumer of that parse, not a new one."""
    from hydromate.progress import SolverProgress

    class Collect(events.NullSink):
        def __init__(self):
            self.progress_calls = []
            self.lines = []

        def line(self, text):
            self.lines.append(text)

        def progress(self, **fields):
            self.progress_calls.append(fields)

    sink = Collect()
    progress = SolverProgress(500.0, sink=sink, echo=False)
    progress.feed("     ITERATION      500    TIME:      0 H  2 MIN  5.0000 S   "
                  "(       125.0000 S)")
    progress.feed("some unremarkable chatter")

    assert len(sink.lines) == 2
    assert len(sink.progress_calls) == 1              # only the header moved the clock
    assert sink.progress_calls[0]["iteration"] == 500
    assert sink.progress_calls[0]["simulated_time"] == pytest.approx(125.0)


def test_the_openfoam_parser_feeds_a_sink_too():
    from hydromate.solvers.openfoam.runtime import OpenFoamProgress

    class Collect(events.NullSink):
        def __init__(self):
            self.calls = []

        def progress(self, **fields):
            self.calls.append(fields)

    sink = Collect()
    progress = OpenFoamProgress(10.0, sink=sink, echo=False)
    progress.feed("Courant Number mean: 0.05 max: 0.42")
    progress.feed("deltaT = 0.0031")
    progress.feed("Time = 2.5")

    assert sink.calls[-1]["simulated_time"] == pytest.approx(2.5)
    assert sink.calls[-1]["courant"] == pytest.approx(0.42)
    assert sink.calls[-1]["time_step"] == pytest.approx(0.0031)


def test_a_failing_sink_never_stops_the_solver():
    """Reporting is not worth losing a run over."""
    from hydromate.progress import SolverProgress

    class Broken(events.NullSink):
        def progress(self, **fields):
            raise RuntimeError("the status volume went away")

        def line(self, text):
            raise RuntimeError("and so did the log")

    progress = SolverProgress(500.0, sink=Broken(), echo=False)
    progress.feed("     ITERATION      500    TIME: (       125.0000 S)")   # must not raise
    assert progress.iteration == 500


# ------------------------------------------------------------------- the status sink


def test_progress_is_throttled_but_a_phase_change_is_not(job, status, monkeypatch):
    """A TELEMAC listing at a few headers a second would otherwise cause several
    fsync'd rewrites a second on a scratch volume."""
    writes = []
    import hydromate.jobs.store as store
    real = store.write_status
    monkeypatch.setattr(store, "write_status",
                        lambda jd, st: (writes.append(1), real(jd, st))[1])

    sink = events.StatusFileSink(job, status, min_interval=1000.0)
    for i in range(50):
        sink.progress(iteration=i)
    assert len(writes) == 1                     # the first only; the rest are throttled

    sink.step("meshing")
    assert len(writes) == 2                     # a phase change is what a dashboard waits for

    # Whatever was throttled away must still reach disk when the run ends - otherwise
    # the last thing a job did would be missing from its own record.
    sink.progress(iteration=99)
    sink.close()
    assert len(writes) == 3
    from hydromate.jobs.store import read_status
    assert read_status(job).progress.as_dict()["iteration"] == 99

    # And a close with nothing pending writes nothing, rather than churning the file.
    sink.close()
    assert len(writes) == 3


def test_progress_fields_merge_rather_than_replace(job, status):
    sink = events.StatusFileSink(job, status, min_interval=0.0)
    sink.progress(iteration=10, simulated_time=100.0)
    sink.progress(iteration=20)
    assert isinstance(status.progress, SolverRunProgress)
    assert (status.progress.iteration, status.progress.simulated_time) == (20, 100.0)


def test_a_status_write_failure_does_not_propagate(job, status, monkeypatch):
    import hydromate.jobs.store as store

    def explode(*args, **kwargs):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(store, "write_status", explode)

    sink = events.StatusFileSink(job, status, min_interval=0.0)
    sink.progress(iteration=1)                  # must not raise
    sink.close()


def test_tee_keeps_going_when_one_sink_fails():
    class Broken(events.NullSink):
        def progress(self, **fields):
            raise RuntimeError("nope")

    class Good(events.NullSink):
        def __init__(self):
            self.seen = 0

        def progress(self, **fields):
            self.seen += 1

    good = Good()
    events.TeeSink(Broken(), good).progress(iteration=1)
    assert good.seen == 1


def test_the_ambient_sink_is_scoped(job, status):
    """A ContextVar, not a module global: nested study steps must not leak their sink
    to a sibling."""
    assert isinstance(events.current_sink.get(), events.NullSink)
    sink = events.StatusFileSink(job, status)
    with events.using_sink(sink):
        assert events.current_sink.get() is sink
    assert isinstance(events.current_sink.get(), events.NullSink)


# --------------------------------------------------------------------- log rotation


def test_rotation_keeps_the_last_parts_not_the_first(tmp_path):
    """The mistake plan §23 warns against is a 'stop writing at N bytes' cap, which
    keeps the beginning - and when a two-day run fails, the beginning is the part
    nobody needs."""
    target = tmp_path / "solver.log"
    with logs.RotatingTextSink(target, max_bytes=2000, backups=2) as sink:
        for i in range(2000):
            sink.write(f"line {i:05d} " + "x" * 60)

    assert target.exists()
    live = target.read_text(encoding="utf-8")
    assert "line 01999" in live, "the newest output must be in the live file"
    assert "line 00000" not in live
    # Bounded: the live file plus at most `backups` rollovers.
    parts = sorted(tmp_path.glob("solver.log*"))
    assert len(parts) <= 3


def test_the_solver_listing_never_leaks_into_the_runner_log(tmp_path):
    runner = tmp_path / "runner.log"
    solver = tmp_path / "solver.log"
    with logs.runner_log(runner):
        logging.getLogger("hydromate.test").info("narration")
        with logs.RotatingTextSink(solver) as sink:
            sink.write("ITERATION 100 - a wall of solver output")

    assert "narration" in runner.read_text(encoding="utf-8")
    assert "ITERATION 100" not in runner.read_text(encoding="utf-8")
    assert "ITERATION 100" in solver.read_text(encoding="utf-8")
    assert "narration" not in solver.read_text(encoding="utf-8")


def test_the_runner_log_handler_is_removed_afterwards(tmp_path):
    before = len(logging.getLogger("hydromate").handlers)
    with logs.runner_log(tmp_path / "runner.log"):
        pass
    assert len(logging.getLogger("hydromate").handlers) == before


# ----------------------------------------------------------------- the disk guard


def test_the_disk_guard_refuses_with_an_actionable_remedy(tmp_path, monkeypatch):
    """Plan §24: turning a mid-run death into a refusal that says what to do."""
    from hydromate.core.errors import EnvironmentError as HydromateEnvironmentError

    monkeypatch.setattr(logs, "free_bytes", lambda path: 1024)
    with pytest.raises(HydromateEnvironmentError) as excinfo:
        logs.require_free_space(tmp_path, minimum=5 * 1024 ** 3)
    assert "job root" in (excinfo.value.remedy or "")
    assert excinfo.value.details["free_bytes"] == 1024


def test_the_disk_guard_is_silent_when_there_is_room(tmp_path):
    logs.require_free_space(tmp_path, minimum=1)


def test_the_disk_guard_does_not_fail_when_it_cannot_tell(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "free_bytes", lambda path: None)
    logs.require_free_space(tmp_path, minimum=5 * 1024 ** 3)


# --------------------------------------------------------------- the study question


def test_policy_ask_answers_from_the_job_and_never_blocks():
    from hydromate.jobs.interaction import PolicyAsk, classify

    assert classify("Reuse the 3 completed levels?") == "resume"
    assert classify("Refine one level further?") == "extend"
    assert classify("Something else entirely?") == "other"

    ask = PolicyAsk({"resume": True, "extend": False})
    assert ask("Reuse the 3 completed levels?") is True
    assert ask("Refine one level further?") is False
    # An unclassified question falls back to the study's own default rather than to a
    # blanket yes.
    assert ask("Proceed with something else?", default=True) is True


def test_an_answer_file_outranks_the_job_record(tmp_path):
    """The file is the more recent human intent, which is what lets a future
    'hydromate answer' reach a running study."""
    import json

    from hydromate.jobs.interaction import PolicyAsk

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"extend": True}), encoding="utf-8")
    ask = PolicyAsk({"extend": False}, answer_file=answers)
    assert ask("Refine one level further?") is True


def test_every_answer_is_recorded_for_audit():
    from hydromate.jobs.interaction import PolicyAsk

    ask = PolicyAsk({"resume": True})
    ask("Reuse the completed levels?")
    prompt, answer, source = ask.asked[0]
    assert answer is True
    assert "job.json" in source
