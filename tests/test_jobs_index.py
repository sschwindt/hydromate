"""The job index: derived, reconstructible, and never fatal.

The governing rule (plan §5) is that the per-job **directory is authoritative** and the
SQLite index is a cache. These tests pin the consequences of that: a rebuild restores the
truth, a disagreement resolves towards the directories, and an index that cannot be
written never takes a job or a listing down with it.
"""

from __future__ import annotations

import sqlite3

import pytest

from axqua.jobs import paths as jobpaths
from axqua.jobs.index import JobIndex, list_jobs, row_for, scan
from axqua.jobs.model import JobKind, JobSpec, JobState, JobStatus
from axqua.jobs.paths import JobDir
from axqua.jobs.store import write_spec, write_status


def _make_job(root, name: str, *, state: JobState = JobState.QUEUED) -> JobDir:
    jd = JobDir(root / name).create()
    spec = JobSpec(job_id=name, kind=JobKind.STEADY_RUN, solver="telemac",
                   case={"name": name.split("-")[3]})
    write_spec(jd, spec)
    status = JobStatus.initial(spec)
    if state is not JobState.QUEUED:
        status.state = state
    write_status(jd, status)
    return jd


@pytest.fixture
def populated(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    _make_job(root, "2026-08-10-alpha-steady-aaaaaa", state=JobState.COMPLETED)
    _make_job(root, "2026-08-11-beta-steady-bbbbbb", state=JobState.FAILED)
    _make_job(root, "2026-08-12-gamma-steady-cccccc", state=JobState.COMPLETED)
    return root


def test_scan_finds_every_job_newest_first(populated):
    rows = list(scan(populated))
    assert [r.job_id.split("-")[3] for r in rows] == ["gamma", "beta", "alpha"]


def test_scan_ignores_directories_that_are_not_jobs(populated):
    """A shared scratch root holds other things; they must not be misread as jobs."""
    (populated / "some-other-folder").mkdir()
    (populated / "2026-08-13-nospec-steady-dddddd").mkdir()
    assert len(list(scan(populated))) == 3


def test_rebuild_reconstructs_the_index_from_the_directories(populated):
    index = JobIndex(populated.parent / "jobs.sqlite")
    assert index.rebuild(populated) == 3
    assert {r.job_id for r in index.list()} == {r.job_id for r in scan(populated)}


def test_a_deleted_directory_disappears_on_rebuild(populated):
    import shutil
    index = JobIndex(populated.parent / "jobs.sqlite")
    index.rebuild(populated)
    shutil.rmtree(populated / "2026-08-11-beta-steady-bbbbbb")
    assert index.rebuild(populated) == 2
    assert all("beta" not in r.job_id for r in index.list())


def test_a_restored_directory_reappears_on_rebuild(populated, tmp_path):
    """Covers the user who moved or restored a job folder by hand."""
    import shutil
    index = JobIndex(populated.parent / "jobs.sqlite")
    index.rebuild(populated)
    moved = tmp_path / "elsewhere"
    shutil.move(str(populated / "2026-08-10-alpha-steady-aaaaaa"), str(moved))
    assert index.rebuild(populated) == 2
    shutil.move(str(moved), str(populated / "2026-08-10-alpha-steady-aaaaaa"))
    assert index.rebuild(populated) == 3


def test_filters_work(populated):
    index = JobIndex(populated.parent / "jobs.sqlite")
    index.rebuild(populated)
    assert len(index.list(state="COMPLETED")) == 2
    assert len(index.list(case="beta")) == 1
    assert len(index.list(limit=1)) == 1


def test_a_corrupt_database_falls_back_to_a_scan(populated, monkeypatch):
    """A slower correct answer beats an error - a fresh checkout, a read-only home and
    a corrupt file all land here."""
    db = populated.parent / "jobs.sqlite"
    db.write_bytes(b"this is not a database" * 100)
    monkeypatch.setenv("AXQUA_DATA_DIR", str(populated.parent))
    rows = list_jobs(job_root=populated)
    assert len(rows) == 3


def test_an_index_write_failure_is_swallowed(populated, monkeypatch):
    """Bookkeeping must never fail a job (plan §24)."""
    index = JobIndex(populated.parent / "jobs.sqlite")
    index.rebuild(populated)

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", explode)
    row = row_for(populated / "2026-08-10-alpha-steady-aaaaaa")
    index.upsert(row)            # must not raise
    assert index.get(row.job_id) is None      # and must not pretend it worked


def test_listing_refreshes_a_non_terminal_row_from_disk(populated):
    """The index is written at submit time and cannot know a detached job has moved on.

    Only the directory knows, and the directory wins.
    """
    index = JobIndex(populated.parent / "jobs.sqlite")
    index.rebuild(populated)
    jd = JobDir(populated / "2026-08-12-gamma-steady-cccccc")

    # Put the index deliberately out of date, the way a real detached run does.
    stale = index.get(jd.job_id)
    stale.state = "RUNNING"
    index.upsert(stale)

    rows = list_jobs(job_root=populated, index=index)
    gamma = next(r for r in rows if r.job_id == jd.job_id)
    assert gamma.state == "COMPLETED"


def test_the_index_lives_under_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AXQUA_DATA_DIR", str(tmp_path / "data"))
    assert jobpaths.index_path() == tmp_path / "data" / "jobs.sqlite"
