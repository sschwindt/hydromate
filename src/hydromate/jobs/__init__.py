"""The job system: identity, persistence, execution and detachment.

QGIS must not own the lifetime of a solver process (plan §1). That single requirement is
what this package exists for: a job is a **directory on disk**, submitted by one process
and executed by another, and everything about it survives the death of whatever created
it.

Layout, and why it is a sibling of ``core/`` rather than inside it:

* :mod:`~hydromate.jobs.model`, :mod:`~hydromate.jobs.ids`, :mod:`~hydromate.jobs.paths`
  are **standard library only**, so the CLI's job verbs stay as cheap as
  ``hydromate status`` already is.
* :mod:`~hydromate.jobs.executor` dispatches to a solver backend, which is exactly what
  ``tests/test_capabilities.py`` forbids anything in ``core/`` from doing. So the job
  system sits beside ``core`` and ``solvers``, importing both.

Attribute access is lazy (PEP 562), matching the top-level package: importing
``hydromate.jobs`` to read a job id must not drag in sqlite, yaml or a solver.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_EXPORTS: dict[str, tuple[str, ...]] = {
    "ids": ("JOB_ID_RE", "is_job_id", "make_job_id", "parse_job_id", "slug"),
    "paths": ("JobDir", "config_dir", "data_dir", "index_path", "job_root",
              "profiles_path"),
    "model": ("JOB_SCHEMA_VERSION", "STATUS_SCHEMA_VERSION", "IllegalTransition",
              "JobKind", "JobSpec", "JobState", "JobStatus", "KIND_META", "KindMeta",
              "Progress", "Resources", "TRANSITIONS", "Workspace", "kinds_for",
              "parse_kind"),
    "store": ("read_spec", "read_status", "write_json_atomic", "write_spec",
              "write_status"),
    "lock": ("JobLock", "LockedError", "LockRecord"),
    "index": ("JobIndex", "JobRow", "list_jobs", "scan"),
    "reaper": ("reconcile",),
    "profiles": ("Profile", "load_profiles", "resolve", "save_profiles"),
    "events": ("ConsoleSink", "EventSink", "NullSink", "StatusFileSink", "TeeSink",
               "TextLogSink", "current_sink", "using_sink"),
    "logs": ("RotatingTextSink", "require_free_space", "runner_log"),
    "interaction": ("PolicyAsk",),
    "results": ("ResultEntry", "ResultManifest", "read_manifest", "write_manifest"),
    "launcher": ("JobLauncher", "LaunchHandle", "LaunchState", "launcher_for",
                 "select_launcher"),
    "executor": ("ExecutionContext", "JobCancelled", "execute"),
    "submit": ("build_spec", "submit_job"),
}

_NAME_TO_MODULE = {name: module
                   for module, names in _EXPORTS.items()
                   for name in names}

# Spelled out rather than computed from _EXPORTS, matching the top-level package: with
# lazy attribute access, a literal __all__ is the only thing linters, type checkers and
# IDEs can read statically. A test keeps the two in step.
__all__ = [
    "ConsoleSink", "EventSink", "ExecutionContext", "IllegalTransition",
    "JOB_ID_RE", "JOB_SCHEMA_VERSION", "JobCancelled", "JobDir", "JobIndex",
    "JobKind", "JobLauncher", "JobLock", "JobRow", "JobSpec", "JobState",
    "JobStatus", "KIND_META", "KindMeta", "LaunchHandle", "LaunchState",
    "LockRecord", "LockedError", "NullSink", "PolicyAsk", "Profile", "Progress",
    "ResultEntry", "ResultManifest", "Resources", "RotatingTextSink",
    "STATUS_SCHEMA_VERSION", "StatusFileSink", "TRANSITIONS", "TeeSink",
    "TextLogSink", "Workspace", "build_spec", "config_dir", "current_sink",
    "data_dir", "execute", "index_path", "is_job_id", "job_root", "kinds_for",
    "launcher_for", "list_jobs", "load_profiles", "make_job_id", "parse_job_id",
    "parse_kind", "profiles_path", "read_manifest", "read_spec", "read_status",
    "reconcile", "require_free_space", "resolve", "runner_log", "save_profiles",
    "scan", "select_launcher", "slug", "submit_job", "using_sink",
    "write_json_atomic", "write_manifest", "write_spec", "write_status",
]


def __getattr__(name: str):
    module = _NAME_TO_MODULE.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))


if TYPE_CHECKING:  # pragma: no cover - for type checkers and IDEs only
    from hydromate.jobs.events import (ConsoleSink, EventSink, NullSink, StatusFileSink,
                                       TeeSink, TextLogSink, current_sink, using_sink)
    from hydromate.jobs.executor import ExecutionContext, JobCancelled, execute
    from hydromate.jobs.ids import (JOB_ID_RE, is_job_id, make_job_id, parse_job_id, slug)
    from hydromate.jobs.index import JobIndex, JobRow, list_jobs, scan
    from hydromate.jobs.interaction import PolicyAsk
    from hydromate.jobs.launcher import (JobLauncher, LaunchHandle, LaunchState,
                                         launcher_for, select_launcher)
    from hydromate.jobs.lock import JobLock, LockedError, LockRecord
    from hydromate.jobs.logs import RotatingTextSink, require_free_space, runner_log
    from hydromate.jobs.model import (JOB_SCHEMA_VERSION, KIND_META, STATUS_SCHEMA_VERSION,
                                      TRANSITIONS, IllegalTransition, JobKind, JobSpec,
                                      JobState, JobStatus, KindMeta, Progress, Resources,
                                      Workspace, kinds_for, parse_kind)
    from hydromate.jobs.paths import (JobDir, config_dir, data_dir, index_path, job_root,
                                      profiles_path)
    from hydromate.jobs.profiles import Profile, load_profiles, resolve, save_profiles
    from hydromate.jobs.reaper import reconcile
    from hydromate.jobs.results import (ResultEntry, ResultManifest, read_manifest,
                                        write_manifest)
    from hydromate.jobs.store import (read_spec, read_status, write_json_atomic,
                                      write_spec, write_status)
    from hydromate.jobs.submit import build_spec, submit_job
