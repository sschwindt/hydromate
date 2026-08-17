"""``<name>.axqua-prj``: a thin pointer, deliberately.

The original brief had this file hold "all user settings and simulation status". Status
is the one thing it must **not** hold. The runner writes status while QGIS is closed, and
a project file that also carried it would guarantee a lost-update race the first time two
QGIS windows were open on the same project - each would write back the state it happened
to have read.

So the division is (plan §3):

* ``case-config.yml`` - the **model**. Versioned with the case, git-friendly.
* ``<name>.axqua-prj`` - which cases belong to this project, which solver profile to
  use, where the job root is, and plugin view state. User-owned, git-friendly, and the
  **plugin is its only writer**.
* ``profiles.yml`` - machine-local solver installs. Never in the project file, because
  those paths are the least portable thing in the system.
* ``job.json`` / ``status.json`` - the runner's, in the job directory.

Paths are stored relative to the project file wherever they sit underneath it, so a
project directory can be moved or shared without every reference breaking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUFFIX = ".axqua-prj"
#: What a project file was called before the rename. Still opened, and saved back under
#: the new suffix - so a project survives without the user having to recreate it.
LEGACY_SUFFIX = ".hydromate-prj"
SCHEMA_VERSION = 1


@dataclass
class AxquaProject:
    """What a project file holds. Note the absence of any job state."""

    path: Path | None = None
    name: str = ""
    cases: list[str] = field(default_factory=list)
    profile: str = ""
    job_root: str = ""
    launcher: str = "auto"
    active_case: str = ""
    view: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # -- paths --------------------------------------------------------------------
    @property
    def base(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def resolve(self, value: str | os.PathLike) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else (self.base / candidate).resolve()

    def store(self, value: str | os.PathLike) -> str:
        """Relative where it can be, absolute where it cannot.

        A case that lives beside the project travels with it; a scratch volume named by
        absolute path is described honestly rather than turned into a lie.
        """
        target = Path(value).expanduser().resolve()
        try:
            return str(target.relative_to(self.base.resolve()))
        except ValueError:
            return str(target)

    def case_paths(self) -> list[Path]:
        return [self.resolve(c) for c in self.cases]

    def active_case_path(self) -> Path | None:
        if self.active_case:
            return self.resolve(self.active_case)
        paths = self.case_paths()
        return paths[0] if paths else None

    # -- editing ------------------------------------------------------------------
    def add_case(self, config: str | os.PathLike) -> str:
        stored = self.store(config)
        if stored not in self.cases:
            self.cases.append(stored)
        if not self.active_case:
            self.active_case = stored
        return stored

    def remove_case(self, config: str | os.PathLike) -> None:
        stored = self.store(config)
        self.cases = [c for c in self.cases if c != stored]
        if self.active_case == stored:
            self.active_case = self.cases[0] if self.cases else ""

    # -- serialisation ------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "cases": list(self.cases),
            "profile": self.profile,
            "job_root": self.job_root,
            "launcher": self.launcher,
            "active_case": self.active_case,
            "view": dict(self.view),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, path: Path | None = None
                  ) -> "AxquaProject":
        version = int(data.get("schema_version", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"this project file was written by a newer aXqua "
                f"(schema {version}; this plugin understands {SCHEMA_VERSION}). "
                "Update the plugin.")
        if "status" in data or "jobs" in data:
            # Older drafts of the format carried job state. Ignore it rather than
            # trusting it: the job directories are authoritative, and a stale copy here
            # would show the user a state that has not been true for days.
            data = {k: v for k, v in data.items() if k not in ("status", "jobs")}
        return cls(
            path=path,
            name=str(data.get("name") or (path.stem if path else "")),
            cases=[str(c) for c in (data.get("cases") or [])],
            profile=str(data.get("profile") or ""),
            job_root=str(data.get("job_root") or ""),
            launcher=str(data.get("launcher") or "auto"),
            active_case=str(data.get("active_case") or ""),
            view=dict(data.get("view") or {}),
            schema_version=version,
        )


def load(path: str | os.PathLike) -> AxquaProject:
    target = Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{target} is not a aXqua project file")
    return AxquaProject.from_dict(payload, path=target)


def save(project: AxquaProject, path: str | os.PathLike | None = None) -> Path:
    """Write the project file atomically.

    Atomic because the user may well have it open in an editor or under version control,
    and a half-written project file is one they would have to repair by hand.
    """
    target = Path(path) if path else project.path
    if target is None:
        raise ValueError("no path to save the project to")
    if target.suffix != SUFFIX:
        # A project opened under the pre-rename suffix is saved back under the new one.
        # The old file is left in place rather than deleted: it is the user's, and a
        # silent removal is not something a save should do.
        target = target.with_suffix(SUFFIX)
    project.path = target
    if not project.name:
        project.name = target.stem
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(json.dumps(project.as_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target
