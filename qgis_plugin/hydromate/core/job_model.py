"""The plugin's view of jobs, and the polling policy that keeps it cheap.

A naive timer over a scratch volume with hundreds of jobs becomes its own performance
problem, so the policy is explicit (plan §18):

* **Poll only what is visible and not terminal.** A COMPLETED job never changes again.
* **Stat before parse.** Compare ``status.json``'s mtime and skip the read when it has
  not moved. This is the single biggest saving, because most polls find nothing new -
  a solver that writes status every two seconds is still idle from the dashboard's point
  of view for the other 90% of the time.
* **Adapt the interval**: ~2 s while something is running and the dock is visible, ~30 s
  when it is hidden, and stop entirely when nothing is running.
* **Never poll the solver's own output.** Only ``status.json``.

Reading ``status.json`` directly rather than shelling out for every row is deliberate:
the file is the runner's published interface, it is written atomically, and a CLI call per
visible row per two seconds would be a process spawn storm. The CLI is used for the
things that need hydromate's own judgement - reconciling a dead process, listing, and
every mutation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
ACTIVE_STATES = {"STARTING", "RUNNING", "POSTPROCESSING", "CANCEL_REQUESTED"}

#: Poll intervals in milliseconds.
INTERVAL_ACTIVE_MS = 2000
INTERVAL_HIDDEN_MS = 30000
INTERVAL_IDLE_MS = 0            # 0 = stop the timer entirely


@dataclass
class Job:
    """One row of the dashboard. Small by construction (plan §26)."""

    job_id: str
    root: Path
    kind: str = ""
    solver: str = ""
    case_name: str = ""
    state: str = "QUEUED"
    created: str = ""
    updated: str = ""
    finished: str = ""
    phase: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    _mtime: float = 0.0

    # -- state --------------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def status_path(self) -> Path:
        return self.root / "status.json"

    # -- display ------------------------------------------------------------------
    @property
    def progress_text(self) -> str:
        """One column, whatever the job kind is.

        This is why progress is typed per kind rather than flattened: a steady run has a
        simulated-time fraction, a convergence study has levels, and a calibration has an
        objective. A single set of columns would leave most of them empty.
        """
        p = self.progress or {}
        kind = str(p.get("kind", ""))
        if kind == "solver_run":
            duration, time = p.get("duration"), p.get("simulated_time")
            if duration and time is not None:
                return f"{min(time / duration, 1.0) * 100:.0f}%  (it {p.get('iteration', 0)})"
            if time is not None:
                return f"t={time:.0f}s  it={p.get('iteration', 0)}"
        if kind == "ladder":
            done, total = p.get("levels_done", 0), p.get("levels_total")
            text = f"level {done}" + (f"/{total}" if total else "")
            if p.get("pending_question"):
                text += "  (question pending)"
            return text
        if kind == "calibration":
            it, mx = p.get("iteration"), p.get("max_iterations")
            best = p.get("best_objective")
            text = f"iter {it or 0}" + (f"/{mx}" if mx else "")
            return text + (f"  best {best:.4g}" if best is not None else "")
        if kind == "preprocessing":
            return f"{p.get('step_name') or ''} {p.get('step', 0)}/{p.get('n_steps', 5)}"
        if kind == "openfoam_build" and p.get("cells"):
            return f"{int(p['cells']):,} cells"
        return self.phase or ""

    @property
    def objective_text(self) -> str:
        best = (self.progress or {}).get("best_objective")
        return f"{best:.4g}" if isinstance(best, (int, float)) else "-"

    @property
    def error_text(self) -> str:
        if not self.error:
            return ""
        parts = [str(self.error.get("message") or "")]
        if self.error.get("remedy"):
            parts.append(str(self.error["remedy"]))
        return "\n".join(p for p in parts if p)

    # -- construction -------------------------------------------------------------
    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Job":
        """From ``hydromate list --json``."""
        return cls(
            job_id=str(row.get("job_id") or ""),
            root=Path(str(row.get("root") or "")),
            kind=str(row.get("kind") or ""),
            solver=str(row.get("solver") or ""),
            case_name=str(row.get("case_name") or ""),
            state=str(row.get("state") or "QUEUED"),
            created=str(row.get("created") or ""),
            updated=str(row.get("updated") or ""),
            finished=str(row.get("finished") or ""),
        )

    def apply_status(self, payload: dict[str, Any]) -> bool:
        """Fold a ``status.json`` document in. True when anything visible changed."""
        before = (self.state, self.phase, json.dumps(self.progress, sort_keys=True))
        self.state = str(payload.get("state") or self.state)
        self.phase = str(payload.get("phase") or "")
        self.progress = dict(payload.get("progress") or {}) \
            if isinstance(payload.get("progress"), dict) else {}
        self.error = dict(payload.get("error") or {})
        self.updated = str(payload.get("updated") or self.updated)
        self.finished = str(payload.get("finished") or self.finished)
        after = (self.state, self.phase, json.dumps(self.progress, sort_keys=True))
        return before != after

    def refresh_from_disk(self) -> bool:
        """Re-read ``status.json``, but only if it moved.

        The mtime check is the polling policy's main saving: most polls of a running job
        find a file that has not been rewritten since the last one, and skipping the
        parse turns the poll into a single ``stat``.
        """
        path = self.status_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if mtime <= self._mtime:
            return False
        self._mtime = mtime
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Caught mid-replace. The next poll will read the complete file; reporting
            # the job as broken here would be wrong and alarming.
            return False
        return self.apply_status(payload) if isinstance(payload, dict) else False


class JobTable:
    """The dashboard's model: jobs, and how often to look at them."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self._by_id: dict[str, Job] = {}

    def __len__(self) -> int:
        return len(self.jobs)

    def __iter__(self):
        return iter(self.jobs)

    def get(self, job_id: str) -> Job | None:
        return self._by_id.get(job_id)

    def replace(self, rows: list[dict]) -> None:
        """Rebuild from a ``list`` call, keeping live progress for jobs we already had.

        Preserving the in-memory progress matters: ``list`` reports the state but not the
        iteration count, so a naive replace would blank the progress column on every
        refresh and make a running job look stalled.
        """
        jobs, by_id = [], {}
        for row in rows:
            job = Job.from_row(row)
            existing = self._by_id.get(job.job_id)
            if existing is not None:
                job.progress = existing.progress
                job.phase = existing.phase
                job.error = existing.error
                job._mtime = existing._mtime
            jobs.append(job)
            by_id[job.job_id] = job
        self.jobs, self._by_id = jobs, by_id

    @property
    def has_active(self) -> bool:
        return any(job.is_active for job in self.jobs)

    def poll(self, visible_ids: list[str] | None = None) -> list[Job]:
        """Refresh the rows worth refreshing. Returns those that changed."""
        changed = []
        wanted = set(visible_ids) if visible_ids is not None else None
        for job in self.jobs:
            if job.is_terminal:
                continue                    # can never change again
            if wanted is not None and job.job_id not in wanted:
                continue                    # not on screen
            if job.refresh_from_disk():
                changed.append(job)
        return changed

    def interval_ms(self, *, visible: bool) -> int:
        """How long until the next poll - or zero, meaning stop."""
        if not self.has_active:
            return INTERVAL_IDLE_MS
        return INTERVAL_ACTIVE_MS if visible else INTERVAL_HIDDEN_MS


def job_root_for(project_root: str | os.PathLike | None) -> str | None:
    return str(project_root) if project_root else None
