"""``results/results.json``: what a job produced, as small structured metadata.

The rule this file exists to enforce is plan §26: the runner and the plugin communicate
through **job metadata, status metadata, file paths and small structured results** - never
through solver field arrays. A TELEMAC result is hundreds of megabytes and a
reconstructed OpenFOAM case is gigabytes; serialising any of that into JSON, or copying
it so the plugin can find it, would be the single easiest way to make the system
unusable.

So a result entry is a **path plus a hint**. The plugin reads the manifest, decides what
to add to the layer tree, and lets QGIS/MDAL open the file itself.

The manifest is written by :meth:`SolverBackend.export_qgis_results`, which is also why
that method takes no PyQGIS: it produces files and describes them, and the plugin does
the loading (plan §14).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axqua.jobs.model import utc_now
from axqua.jobs.paths import JobDir
from axqua.jobs.store import write_json_atomic

RESULTS_SCHEMA_VERSION = 1

__all__ = ["ResultEntry", "ResultManifest", "read_manifest", "write_manifest"]

#: How the plugin should present an entry. Free-form on purpose - a hint the loader may
#: ignore, not a contract it must satisfy, so a new style never breaks an older plugin.
STYLE_DEPTH = "water-depth"
STYLE_VELOCITY = "velocity"
STYLE_NONE = ""


@dataclass
class ResultEntry:
    """One loadable artifact."""

    name: str
    path: str
    kind: str = "file"          # mesh | raster | vector | table | figure | file
    style: str = STYLE_NONE
    variable: str = ""
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "path": self.path, "kind": self.kind}
        for key in ("style", "variable", "description"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultEntry":
        return cls(
            name=str(data.get("name") or Path(str(data.get("path", ""))).name),
            path=str(data.get("path", "")),
            kind=str(data.get("kind") or "file"),
            style=str(data.get("style") or ""),
            variable=str(data.get("variable") or ""),
            description=str(data.get("description") or ""),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class ResultManifest:
    """Everything one job produced that is worth opening."""

    job_id: str = ""
    solver: str = ""
    kind: str = ""
    generated: str = field(default_factory=utc_now)
    schema_version: int = RESULTS_SCHEMA_VERSION
    objective: float | None = None
    entries: list[ResultEntry] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, path: str | os.PathLike, *, kind: str = "file",
            style: str = STYLE_NONE, variable: str = "", description: str = "",
            base: Path | None = None, **extra: Any) -> "ResultManifest":
        """Record one artifact, skipping it if the file is not there.

        Silently skipping is right here: backends list everything they *might* have
        produced, and a manifest entry pointing at a missing file would make the plugin
        show a broken layer rather than simply not showing one.
        """
        target = Path(path)
        if not target.exists():
            return self
        stored = target
        if base is not None:
            # Relative paths keep a job directory movable: an archive restored under a
            # different mount must still open.
            try:
                stored = target.relative_to(base)
            except ValueError:
                stored = target
        self.entries.append(ResultEntry(name=name, path=str(stored), kind=kind,
                                        style=style, variable=variable,
                                        description=description, extra=dict(extra)))
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "solver": self.solver,
            "kind": self.kind,
            "generated": self.generated,
            "objective": self.objective,
            "summary": dict(self.summary),
            "results": [e.as_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultManifest":
        return cls(
            job_id=str(data.get("job_id") or ""),
            solver=str(data.get("solver") or ""),
            kind=str(data.get("kind") or ""),
            generated=str(data.get("generated") or utc_now()),
            schema_version=int(data.get("schema_version", RESULTS_SCHEMA_VERSION)),
            objective=data.get("objective"),
            entries=[ResultEntry.from_dict(e) for e in (data.get("results") or [])],
            summary=dict(data.get("summary") or {}),
        )


def write_manifest(job_dir: JobDir | str | os.PathLike,
                   manifest: ResultManifest) -> Path:
    jd = job_dir if isinstance(job_dir, JobDir) else JobDir(job_dir)
    jd.results.mkdir(parents=True, exist_ok=True)
    return write_json_atomic(jd.results_json, manifest.as_dict())


def read_manifest(job_dir: JobDir | str | os.PathLike) -> ResultManifest | None:
    from axqua.jobs.store import read_json_tolerant
    jd = job_dir if isinstance(job_dir, JobDir) else JobDir(job_dir)
    payload = read_json_tolerant(jd.results_json)
    return ResultManifest.from_dict(payload) if payload else None
