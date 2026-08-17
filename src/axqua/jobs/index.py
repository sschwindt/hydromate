"""A SQLite index over the job root - derived, never authoritative.

Listing hundreds of jobs by opening hundreds of ``status.json`` files is slow enough to
be felt in a dashboard, so there is an index. But the index is a **cache of the
directories**, and the directories win every disagreement (plan §5). Two consequences,
both deliberate:

* ``rebuild()`` reconstructs the whole thing by scanning, and it is a *tested* path
  rather than a repair tool nobody exercises. That covers the first crash mid-write, a
  restored backup, and a job directory the user moved by hand.
* **Every index failure is swallowed.** A job must never fail because its bookkeeping
  could not be written; that is what makes plan §24's "a solver failure must not corrupt
  the job registry" true by construction rather than by care. Callers that cannot open
  the database fall back to a live directory scan.

WAL journalling, so a QGIS reader polling the list never blocks the runner writing it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from axqua.jobs import paths as jobpaths
from axqua.jobs.ids import JOB_ID_RE
from axqua.jobs.paths import JobDir

log = logging.getLogger("axqua.jobs.index")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id         TEXT PRIMARY KEY,
    root           TEXT NOT NULL,
    kind           TEXT,
    solver         TEXT,
    case_name      TEXT,
    source_config  TEXT,
    state          TEXT,
    created        TEXT,
    updated        TEXT,
    finished       TEXT,
    schema_version INTEGER,
    indexed        TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state   ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
CREATE INDEX IF NOT EXISTS jobs_case    ON jobs(case_name);
"""


@dataclass
class JobRow:
    """One row, flattened for a table view. Never carries solver field data (plan §26)."""

    job_id: str
    root: str
    kind: str | None = None
    solver: str | None = None
    case_name: str | None = None
    source_config: str | None = None
    state: str | None = None
    created: str | None = None
    updated: str | None = None
    finished: str | None = None
    schema_version: int | None = None
    indexed: str | None = None

    @property
    def job_dir(self) -> JobDir:
        return JobDir(self.root)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "root": self.root, "kind": self.kind,
            "solver": self.solver, "case_name": self.case_name,
            "source_config": self.source_config, "state": self.state,
            "created": self.created, "updated": self.updated,
            "finished": self.finished,
        }


class JobIndex:
    """The index. Constructing one never raises; using one never raises."""

    def __init__(self, db_path: str | os.PathLike | None = None) -> None:
        self.path = Path(db_path) if db_path else jobpaths.index_path()
        self._ok = self._ensure_schema()

    @property
    def usable(self) -> bool:
        """False when the database could not be opened - callers then scan instead."""
        return self._ok

    # -- plumbing -----------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection | None]:
        if not self._ok:
            yield None
            return
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            log.debug("job index unavailable (%s): %s", type(exc).__name__, exc)
            yield None
        finally:
            if conn is not None:
                with closing(conn):
                    pass

    def _ensure_schema(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5.0)) as conn:
                # WAL: the dashboard reads while the runner writes, and a reader that
                # blocks the runner would be worse than no index at all.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            return True
        except (sqlite3.Error, OSError) as exc:
            log.debug("job index disabled (%s): %s", type(exc).__name__, exc)
            return False

    # -- writes -------------------------------------------------------------------
    def upsert(self, row: JobRow) -> None:
        from axqua.jobs.model import utc_now
        with self._connect() as conn:
            if conn is None:
                return
            conn.execute(
                "INSERT INTO jobs (job_id, root, kind, solver, case_name, source_config,"
                " state, created, updated, finished, schema_version, indexed)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " root=excluded.root, kind=excluded.kind, solver=excluded.solver,"
                " case_name=excluded.case_name, source_config=excluded.source_config,"
                " state=excluded.state, created=excluded.created,"
                " updated=excluded.updated, finished=excluded.finished,"
                " schema_version=excluded.schema_version, indexed=excluded.indexed",
                (row.job_id, str(row.root), row.kind, row.solver, row.case_name,
                 row.source_config, row.state, row.created, row.updated, row.finished,
                 row.schema_version, utc_now()),
            )

    def remove(self, job_id: str) -> None:
        with self._connect() as conn:
            if conn is not None:
                conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    # -- reads --------------------------------------------------------------------
    def get(self, job_id: str) -> JobRow | None:
        with self._connect() as conn:
            if conn is None:
                return None
            cur = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            found = cur.fetchone()
        return _row(found) if found else None

    def list(self, *, state: str | None = None, kind: str | None = None,
             solver: str | None = None, case: str | None = None,
             since: str | None = None, limit: int | None = None) -> list[JobRow]:
        clauses, args = [], []
        for column, value in (("state", state), ("kind", kind), ("solver", solver),
                              ("case_name", case)):
            if value:
                clauses.append(f"{column} = ?")
                args.append(value)
        if since:
            clauses.append("created >= ?")
            args.append(since)
        sql = "SELECT * FROM jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created DESC, job_id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            if conn is None:
                return []
            rows = conn.execute(sql, args).fetchall()
        return [_row(r) for r in rows]

    # -- the repair path ----------------------------------------------------------
    def rebuild(self, job_root: str | os.PathLike | None = None, *,
                reconcile: bool = True) -> int:
        """Rebuild from a directory scan. Returns how many jobs were found.

        This is the authority restoring the cache, so it clears first: an index row for
        a directory the user deleted is exactly the kind of disagreement the scan exists
        to settle.
        """
        root = Path(job_root) if job_root else jobpaths.job_root()
        rows = list(scan(root, reconcile=reconcile))
        with self._connect() as conn:
            if conn is not None:
                conn.execute("DELETE FROM jobs")
        for row in rows:
            self.upsert(row)
        return len(rows)


def _row(record: sqlite3.Row) -> JobRow:
    return JobRow(**{k: record[k] for k in record.keys()})


def row_for(job_dir: JobDir | str | os.PathLike, *, reconcile: bool = False
            ) -> JobRow | None:
    """Read one job directory into a row, or ``None`` if it is not a job.

    Never raises: a half-written or hand-mangled directory should drop out of a listing,
    not take the listing down with it.
    """
    jd = job_dir if isinstance(job_dir, JobDir) else JobDir(job_dir)
    try:
        from axqua.jobs.store import read_spec, read_status
        spec = read_spec(jd)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        log.debug("skipping %s: %s", jd.root, exc)
        return None
    status = None
    try:
        if reconcile:
            from axqua.jobs.reaper import reconcile as _reconcile
            status = _reconcile(jd)
        else:
            status = read_status(jd)
    except Exception as exc:                      # noqa: BLE001
        log.debug("could not read status of %s: %s", jd.root, exc)
    return JobRow(
        job_id=spec.job_id,
        root=str(jd.root),
        kind=spec.kind.value,
        solver=spec.solver,
        case_name=spec.case_name,
        source_config=spec.source_config,
        state=status.state.value if status else "QUEUED",
        created=spec.created,
        updated=status.updated if status else spec.created,
        finished=status.finished if status else None,
        schema_version=spec.schema_version,
    )


def scan(job_root: str | os.PathLike | None = None, *, reconcile: bool = False
         ) -> Iterator[JobRow]:
    """Yield every job directory under *job_root*, newest first.

    The directory name must look like a job id, so unrelated folders under a shared
    scratch root are skipped rather than misread.
    """
    root = Path(job_root) if job_root else jobpaths.job_root()
    if not root.is_dir():
        return
    entries = sorted((p for p in root.iterdir()
                      if p.is_dir() and JOB_ID_RE.match(p.name)),
                     key=lambda p: p.name, reverse=True)
    for entry in entries:
        row = row_for(entry, reconcile=reconcile)
        if row is not None:
            yield row


def list_jobs(*, job_root: str | os.PathLike | None = None,
              index: JobIndex | None = None, rebuild: bool = False,
              **filters: Any) -> list[JobRow]:
    """List jobs, preferring the index and falling back to a scan.

    The fallback is not a corner case: a fresh checkout, a read-only home, or a corrupt
    database all land here, and in every one of them a slower correct answer beats an
    error.
    """
    root = Path(job_root) if job_root else jobpaths.job_root()
    idx = index if index is not None else JobIndex()
    if rebuild:
        idx.rebuild(root)
    if idx.usable:
        rows = idx.list(**filters)
        if rows or not root.is_dir():
            return _refresh(rows, idx)
        # An empty index over a populated root means the index has not seen these jobs
        # yet (a job root moved in, say). Scan, and take the opportunity to catch up.
        found = list(scan(root, reconcile=True))
        for row in found:
            idx.upsert(row)
        return _filter(found, **filters)
    return _filter(list(scan(root, reconcile=True)), **filters)


def _refresh(rows: list[JobRow], idx: JobIndex) -> list[JobRow]:
    """Re-read the non-terminal rows from their directories.

    The index is written at submit time and cannot know that a detached job has since
    progressed or died - only the directory knows, and the directory is authoritative.
    A terminal row is skipped because it can never change again, which keeps a listing of
    hundreds of finished jobs as cheap as a single query.
    """
    from axqua.jobs.model import JobState

    out: list[JobRow] = []
    for row in rows:
        try:
            terminal = JobState(row.state or "QUEUED").is_terminal
        except ValueError:
            terminal = False
        if terminal:
            out.append(row)
            continue
        fresh = row_for(row.root, reconcile=True)
        if fresh is None:
            # The directory is gone. The row is dropped from this listing and from the
            # index, since the directory is what makes a job real.
            idx.remove(row.job_id)
            continue
        if fresh.state != row.state:
            idx.upsert(fresh)
        out.append(fresh)
    return out


def _filter(rows: list[JobRow], *, state: str | None = None, kind: str | None = None,
            solver: str | None = None, case: str | None = None,
            since: str | None = None, limit: int | None = None) -> list[JobRow]:
    out = rows
    if state:
        out = [r for r in out if r.state == state]
    if kind:
        out = [r for r in out if r.kind == kind]
    if solver:
        out = [r for r in out if r.solver == solver]
    if case:
        out = [r for r in out if r.case_name == case]
    if since:
        out = [r for r in out if (r.created or "") >= since]
    return out[:limit] if limit else out
