"""Atomic writes, tolerant reads, the lock, and schema migration.

The concurrency tests here are the point: QGIS polls ``status.json`` while the runner
rewrites it, and "usually fine" is not good enough when the consequence is a dashboard
that reports a job as broken because it caught a half-written file.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from axqua.core.errors import ConfigError
from axqua.jobs import procs
from axqua.jobs.lock import JobLock, LockedError
from axqua.jobs.model import (JOB_SCHEMA_VERSION, JobKind, JobSpec, JobState,
                                  JobStatus, SteadyRunOptions)
from axqua.jobs.paths import JobDir
from axqua.jobs.store import (read_json_tolerant, read_spec, read_status,
                                  write_json_atomic, write_spec, write_status)


@pytest.fixture
def job(tmp_path) -> JobDir:
    return JobDir(tmp_path / "2026-08-14-demo-steady-a1b2c3").create()


@pytest.fixture
def spec() -> JobSpec:
    return JobSpec(job_id="2026-08-14-demo-steady-a1b2c3", kind=JobKind.STEADY_RUN,
                   solver="telemac", case={"name": "demo"},
                   options=SteadyRunOptions(ncsize=2))


# --------------------------------------------------------------------- atomic write


def test_a_reader_never_sees_a_partial_file(tmp_path):
    """500 rewrites under a concurrent reader; every read must parse.

    This is the exact race a polling dashboard hits, and the reason the write is
    tmp + fsync + os.replace rather than a plain open-and-write.
    """
    target = tmp_path / "status.json"
    write_json_atomic(target, {"n": 0})
    failures: list[Exception] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                failures.append(AssertionError("the file disappeared mid-replace"))
            except json.JSONDecodeError as exc:
                failures.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for n in range(500):
            write_json_atomic(target, {"n": n, "padding": "x" * 4000})
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not failures


def test_no_temporary_files_are_left_behind(tmp_path):
    write_json_atomic(tmp_path / "a.json", {"k": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_read_json_tolerant_returns_none_for_an_absent_file(tmp_path):
    """An absent status.json is a normal state - the job exists but has not run."""
    assert read_json_tolerant(tmp_path / "nope.json") is None


def test_read_json_tolerant_eventually_gives_up_with_a_useful_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        read_json_tolerant(bad, retries=2, delay=0)
    assert "rebuild" in (excinfo.value.remedy or "")


# ------------------------------------------------------------------- the two records


def test_the_submission_record_is_written_once(job, spec):
    write_spec(job, spec)
    with pytest.raises(ConfigError) as excinfo:
        write_spec(job, spec)
    assert "written once" in str(excinfo.value)


def test_the_submission_record_round_trips(job, spec):
    write_spec(job, spec)
    restored = read_spec(job)
    assert restored.job_id == spec.job_id
    assert restored.options.ncsize == 2


def test_reading_a_directory_that_is_not_a_job_says_so(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        read_spec(tmp_path)
    assert "not a axqua job" in (excinfo.value.remedy or "")


def test_status_round_trips(job, spec):
    status = JobStatus.initial(spec)
    status.transition(JobState.STARTING)
    write_status(job, status)
    assert read_status(job).state is JobState.STARTING


# ------------------------------------------------------------------------ migration


def test_a_newer_schema_is_refused_and_both_versions_are_named(job, spec):
    write_spec(job, spec)
    payload = json.loads(job.spec.read_text(encoding="utf-8"))
    payload["schema_version"] = JOB_SCHEMA_VERSION + 5
    job.spec.chmod(0o644)
    job.spec.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        read_spec(job)
    assert excinfo.value.details["found"] == JOB_SCHEMA_VERSION + 5
    assert excinfo.value.details["supported"] == JOB_SCHEMA_VERSION


def test_an_older_schema_migrates_in_memory_and_is_not_rewritten(job, spec, monkeypatch):
    """A read must not mutate a record the user may still want to inspect."""
    from axqua.jobs import store

    monkeypatch.setitem(store.JOB_MIGRATIONS, 0, lambda d: {**d, "solver": "telemac"})
    payload = spec.as_dict()
    payload["schema_version"] = 0
    job.spec.write_text(json.dumps(payload), encoding="utf-8")
    before = job.spec.read_text(encoding="utf-8")

    restored = read_spec(job)
    assert restored.solver == "telemac"
    assert job.spec.read_text(encoding="utf-8") == before


# ----------------------------------------------------------------------------- lock


def test_a_second_holder_is_refused(job):
    lock = JobLock(job)
    lock.acquire()
    try:
        with pytest.raises(LockedError):
            JobLock(job).acquire(break_stale=False)
    finally:
        lock.release()


def test_a_lock_whose_process_is_gone_is_stale_and_can_be_broken(job):
    """Otherwise a job that crashed at 3am could never be resubmitted."""
    JobLock(job).acquire()
    payload = json.loads(job.lock.read_text(encoding="utf-8"))
    payload["pid"] = _dead_pid()
    payload["proc_started"] = None
    job.lock.write_text(json.dumps(payload), encoding="utf-8")

    stale, reason = JobLock(job).is_stale()
    assert stale and "gone" in reason
    JobLock(job).acquire(break_stale=True).release()


def test_pid_reuse_is_detected(job):
    """A live PID whose start time differs is a *different* process, so the lock is
    stale even though something with that number is running."""
    JobLock(job).acquire()
    payload = json.loads(job.lock.read_text(encoding="utf-8"))
    payload["proc_started"] = (payload.get("proc_started") or 0.0) + 12345.0
    job.lock.write_text(json.dumps(payload), encoding="utf-8")

    stale, reason = JobLock(job).is_stale()
    assert stale and "reuse" in reason


def test_a_lock_held_on_another_machine_is_never_broken_automatically(job):
    """On a shared job root the PID means nothing here, so the honest answer is
    'cannot tell' and breaking it is a human decision."""
    JobLock(job).acquire()
    payload = json.loads(job.lock.read_text(encoding="utf-8"))
    payload["host"] = "some-other-host"
    job.lock.write_text(json.dumps(payload), encoding="utf-8")

    stale, reason = JobLock(job).is_stale()
    assert not stale
    assert "another machine" in reason
    with pytest.raises(LockedError):
        JobLock(job).acquire()
    JobLock(job).acquire(force=True).release()


def test_a_reboot_makes_every_recorded_pid_stale(job, monkeypatch):
    JobLock(job).acquire()
    payload = json.loads(job.lock.read_text(encoding="utf-8"))
    payload["boot_id"] = "a-previous-boot"
    job.lock.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(procs, "boot_id", lambda: "the-current-boot")
    stale, reason = JobLock(job).is_stale()
    assert stale and "restarted" in reason


def test_an_unreadable_lock_file_is_treated_as_a_crash(job):
    job.lock.write_text("truncated{", encoding="utf-8")
    stale, reason = JobLock(job).is_stale()
    assert stale and "unreadable" in reason


def test_holding_releases_even_on_failure(job):
    with pytest.raises(RuntimeError):
        with JobLock(job).holding():
            raise RuntimeError("boom")
    assert not job.lock.exists()


def _dead_pid() -> int:
    """A PID that is very unlikely to exist. Verified, not assumed."""
    for candidate in range(4_000_000, 4_000_100):
        if not procs.pid_alive(candidate):
            return candidate
    pytest.skip("could not find an unused pid")


# ---------------------------------------------------------------------------- procs


def test_the_current_process_is_alive_and_has_a_start_time():
    assert procs.pid_alive(os.getpid())
    first = procs.process_start_time(os.getpid())
    time.sleep(0.01)
    assert procs.process_start_time(os.getpid()) == first   # stable, not a clock read


def test_a_dead_pid_is_not_alive():
    assert not procs.pid_alive(_dead_pid())
    assert not procs.pid_alive(0)
    assert not procs.pid_alive(None)
