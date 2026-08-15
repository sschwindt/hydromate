"""The job model: states, kinds, options, progress, and the two records on disk.

Two files describe a job, and the split is the whole point (plan §3):

``job.json``
    The **immutable submission record**. What was asked for, *resolved and frozen* at
    submit time - including the entire case configuration, not a reference to it. If the
    user edits ``case-config.yml`` afterwards, a running job must not change underneath
    itself, and a finished job must stay reproducible from its own directory alone.
    Written once, then made read-only.

``status.json``
    The **mutable state**. Written only by the runner, only by atomic replace. Readers
    never lock; they read the last complete file.

Progress is **nested and typed by job kind**, not flattened into the top level. Most jobs
are not optimizations: a steady run reports simulated time against duration, a
mesh-convergence study reports levels completed, a BAL run reports iterations and a best
objective. Hoisting ``iteration`` and ``best_objective`` to the top level would force
every other kind to publish nulls and would invite the dashboard to render meaningless
zeros.

Standard library only. The CLI touches this module on every job verb, and
``tests/test_capabilities.py`` asserts from a subprocess that nothing heavy is pulled in.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any

from hydromate.core.capabilities import Capability
from hydromate.core.errors import ConfigError

JOB_SCHEMA_VERSION = 1
STATUS_SCHEMA_VERSION = 1


def utc_now() -> str:
    """One timestamp format everywhere: ISO-8601, seconds, with an offset."""
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- states


class JobState(str, Enum):
    """Where a job is. ``str``-valued so it serialises without a converter."""

    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    POSTPROCESSING = "POSTPROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:  # so f-strings and log lines read cleanly
        return self.value

    @property
    def is_terminal(self) -> bool:
        """A terminal job never changes again - which is what lets the dashboard stop
        polling it (plan §18)."""
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        return self in _ACTIVE

    def can_go_to(self, other: "JobState") -> bool:
        return other in TRANSITIONS[self]


_TERMINAL = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})
_ACTIVE = frozenset({JobState.STARTING, JobState.RUNNING, JobState.POSTPROCESSING})

#: The legal edges. Two are worth spelling out because they look wrong at a glance:
#:
#: * ``QUEUED -> CANCELLED`` directly - the job was cancelled before it ever launched, so
#:   there is nothing to tear down and no reason to pass through CANCEL_REQUESTED.
#: * ``CANCEL_REQUESTED -> COMPLETED`` - a cancel is a *request*; a solver a few seconds
#:   from finishing may well finish first, and reporting that as CANCELLED would be a lie.
TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.STARTING, JobState.CANCEL_REQUESTED,
                                JobState.CANCELLED, JobState.FAILED}),
    JobState.STARTING: frozenset({JobState.RUNNING, JobState.FAILED,
                                  JobState.CANCEL_REQUESTED, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.POSTPROCESSING, JobState.COMPLETED,
                                 JobState.FAILED, JobState.CANCEL_REQUESTED}),
    JobState.POSTPROCESSING: frozenset({JobState.COMPLETED, JobState.FAILED,
                                        JobState.CANCEL_REQUESTED}),
    JobState.CANCEL_REQUESTED: frozenset({JobState.CANCELLED, JobState.COMPLETED,
                                          JobState.FAILED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class IllegalTransition(ConfigError):
    """Raised rather than coerced.

    Silently clamping an impossible transition is how a job ends up reporting RUNNING
    forever, or COMPLETED after a crash. The state machine is small enough that any
    violation is a bug in hydromate, and it should be loud.
    """

    code = "hydromate.job.transition"


def check_transition(current: JobState, target: JobState) -> None:
    if current is target:
        return
    if not current.can_go_to(target):
        raise IllegalTransition(
            f"cannot move a job from {current} to {target}",
            subject="state",
            current=current.value,
            target=target.value,
            allowed=sorted(s.value for s in TRANSITIONS[current]),
        )


# ---------------------------------------------------------------------------- kinds


class JobKind(str, Enum):
    """One kind per case-template script.

    The taxonomy is not invented here - ``cases/case-template/`` already contains it, one
    script per unit of long-running work. Keeping the two in step means a user who knows
    the scripts knows the job kinds, and the scripts stay the supported standalone path
    (plan §29).
    """

    PREPROCESSING = "preprocessing"
    STEADY_RUN = "steady"
    MESH_CONVERGENCE = "mesh-convergence"
    BUILD_3D = "build-3d"
    STEADY_RUN_3D = "steady-3d"
    VERTICAL_CONVERGENCE = "vertical-convergence"
    UNSTEADY_RUN = "unsteady"
    OPENFOAM_BUILD = "openfoam-build"
    OPENFOAM_RUN = "openfoam-run"
    CALIBRATION = "calibration"
    CALIBRATION_MULTIFLOW = "calibration-multiflow"

    def __str__(self) -> str:
        return self.value


def parse_kind(value: str) -> JobKind:
    """Accept the enum value, the enum name, or a few obvious aliases."""
    text = str(value).strip().lower().replace("_", "-")
    for kind in JobKind:
        if text in (kind.value, kind.name.lower().replace("_", "-")):
            return kind
    aliases = {"preprocess": JobKind.PREPROCESSING, "build": JobKind.PREPROCESSING,
               "steady2d": JobKind.STEADY_RUN, "run": JobKind.STEADY_RUN,
               "meshconv": JobKind.MESH_CONVERGENCE, "3d": JobKind.BUILD_3D,
               "steady3d": JobKind.STEADY_RUN_3D, "vertconv": JobKind.VERTICAL_CONVERGENCE,
               "bal": JobKind.CALIBRATION, "balmf": JobKind.CALIBRATION_MULTIFLOW,
               "of-build": JobKind.OPENFOAM_BUILD, "of-run": JobKind.OPENFOAM_RUN}
    if text in aliases:
        return aliases[text]
    raise ConfigError(
        f"unknown job kind {value!r}",
        subject="kind",
        remedy="Known kinds: " + ", ".join(k.value for k in JobKind),
    )


# -------------------------------------------------------------------------- options
#
# Each options dataclass is the schema of one case-template script's module-level
# constants. That is the answer to "do not copy-paste the scripts": the knobs move into
# the job record, the orchestration functions stay exactly where they are.


@dataclass
class Options:
    """Base for the per-kind option records.

    A dataclass in its own right, with no fields, so that it also serves as the "no
    options" case: :class:`JobSpec` defaults to one, and ``as_dict``/``as_kwargs`` then
    return an empty mapping instead of raising.

    ``from_dict`` refuses unknown keys. Plan §5 is explicit: never silently ignore an
    unknown field, because that is how a job that *looks* submitted quietly runs
    something else.
    """

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, where: str = "options") -> "Options":
        data = dict(data or {})
        known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                f"unknown option{'s' if len(unknown) > 1 else ''} for this job kind: "
                + ", ".join(unknown),
                subject=f"{where}.{unknown[0]}",
                remedy="Known options: " + ", ".join(sorted(known)),
            )
        return cls(**data)  # type: ignore[call-arg]

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}  # type: ignore[arg-type]

    def as_kwargs(self) -> dict[str, Any]:
        """What the executor splats into the backend verb."""
        return self.as_dict()


@dataclass
class PreprocessingOptions(Options):
    """``cases/case-template/preprocessing.py``."""

    discharge: float | None = None
    validate_env: bool = True
    dry_run: bool = False


@dataclass
class SteadyRunOptions(Options):
    """``initial_run.py`` - its ``NCSIZE`` and the hotstart tolerance."""

    ncsize: int | None = None
    cas_file: str | None = None
    hotstart_tolerance: float | None = None
    report_wetting: bool = True
    report_sections: bool = False


@dataclass
class MeshConvergenceOptions(Options):
    """``mesh_convergence_study.py``.

    ``answers`` is what replaces the study's stdin prompt in a detached run; see
    :mod:`hydromate.jobs.interaction`.
    """

    discharge: float | None = None
    refinement_ratio: float = 1.3
    tolerance: float = 0.05
    n_coarser: int = 1
    n_finer: int = 2
    max_extra_levels: int = 3
    auto_extend: bool = False
    initial_depth: float | None = 0.5
    ncsize: int | None = None
    answers: dict[str, bool] = field(default_factory=lambda: {"resume": True,
                                                             "extend": False})


@dataclass
class Build3DOptions(Options):
    """``add3d.py`` (build half)."""

    hydrostatic_steps: int | None = None
    n_levels: int | None = None


@dataclass
class Steady3DOptions(Options):
    """``add3d.py --run``."""

    variant: str = "hydrostatic"          # hydrostatic | hydrodyn | unsteady
    ncsize: int | None = None


@dataclass
class VerticalConvergenceOptions(Options):
    """``vertical_convergence_3d.py``."""

    layer_counts: list[int] | None = None
    tolerance: float = 0.05
    ncsize: int | None = None


@dataclass
class UnsteadyOptions(Options):
    """``unsteady_run.py`` and its in-script toggles."""

    mode_3d: bool = False
    control_sections: bool = True
    gaia_enabled: bool = False
    gaia_bedload: bool = True
    gaia_suspended: bool = False
    run: bool = True
    ncsize: int | None = None


@dataclass
class OpenFoamBuildOptions(Options):
    """``openfoam_preprocessing.py``."""

    pre_run: bool | None = None
    hotstart: str | None = None
    cell_size: float | None = None
    layers: int | None = None


@dataclass
class OpenFoamRunOptions(Options):
    """``openfoam_run.py``."""

    n_processors: int | None = None
    reconstruct: bool = True
    to_vtk: bool | None = None
    check_mesh: bool = True


@dataclass
class CalibrationOptions(Options):
    """``run_Bayes_cal.py``."""

    prepare_only: bool = False
    calibration_quantities: list[str] | None = None
    extraction_quantities: list[str] | None = None
    vel_err_floor: float | None = None
    depth_err_floor: float | None = None


@dataclass
class MultiflowCalibrationOptions(Options):
    """``run_Bayes_cal_multiflow.py``.

    ``flows`` carries the ``FlowSpec`` fields as plain dicts; the backend rebuilds the
    dataclasses. Keeping them dicts here is what lets ``job.json`` stay JSON.
    """

    flows: list[dict[str, Any]] = field(default_factory=list)
    launch_mode: str = "run"              # prepare | smoke | run | resume
    force: bool = False


# -------------------------------------------------------------------------- progress


@dataclass
class Progress:
    """Base for the kind-typed progress payloads.

    The ``kind`` discriminator is written into the JSON so a reader can dispatch, and
    :meth:`from_dict` keeps an **unknown** kind as a raw dict rather than discarding it -
    a plugin built against an older hydromate must degrade to showing nothing useful, not
    to losing the field.
    """

    kind: str = "none"

    def as_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["kind"] = self.kind
        return out

    def merged(self, **updates: Any) -> "Progress":
        known = {f.name for f in fields(self)} - {"kind"}
        return dataclasses.replace(self, **{k: v for k, v in updates.items()
                                            if k in known and v is not None})

    @staticmethod
    def from_dict(data: Any) -> "Progress | dict[str, Any] | None":
        if not isinstance(data, dict):
            return None
        cls = _PROGRESS_TYPES.get(str(data.get("kind", "")))
        if cls is None:
            return dict(data)          # forward compatible: keep what we cannot type
        known = {f.name for f in fields(cls)} - {"kind"}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PreprocessingProgress(Progress):
    kind: str = "preprocessing"
    step: int = 0
    n_steps: int = 5
    step_name: str = ""


@dataclass
class SolverRunProgress(Progress):
    """A TELEMAC or OpenFOAM march.

    Progress is measured against *simulated time*, not step count: the CFL-adaptive time
    step makes the number of steps unknown a priori, which is exactly why
    ``hydromate.progress.SolverProgress`` already works this way.
    """

    kind: str = "solver_run"
    simulated_time: float | None = None
    duration: float | None = None
    iteration: int | None = None
    time_step: float | None = None
    courant: float | None = None
    flux_imbalance: float | None = None

    @property
    def fraction(self) -> float | None:
        if not self.duration or self.simulated_time is None:
            return None
        return max(0.0, min(1.0, self.simulated_time / self.duration))


@dataclass
class LadderProgress(Progress):
    """A convergence study: mesh levels or vertical layer counts."""

    kind: str = "ladder"
    levels_done: int = 0
    levels_total: int | None = None
    current_label: str = ""
    converged: bool | None = None
    gci: float | None = None
    pending_question: str | None = None


@dataclass
class CalibrationProgress(Progress):
    kind: str = "calibration"
    iteration: int | None = None
    max_iterations: int | None = None
    best_objective: float | None = None
    flow: str | None = None
    n_flows: int | None = None


@dataclass
class OpenFoamBuildProgress(Progress):
    kind: str = "openfoam_build"
    cells: int | None = None
    stage: str = ""
    mesh_usable: bool | None = None


_PROGRESS_TYPES: dict[str, type[Progress]] = {
    "none": Progress,
    "preprocessing": PreprocessingProgress,
    "solver_run": SolverRunProgress,
    "ladder": LadderProgress,
    "calibration": CalibrationProgress,
    "openfoam_build": OpenFoamBuildProgress,
}


# ------------------------------------------------------------------------ kind meta


@dataclass(frozen=True)
class KindMeta:
    """Everything the system needs to know about a job kind, as data.

    ``verb`` is the :class:`~hydromate.core.registry.SolverBackend` method the executor
    calls. Three verbs rather than one per kind: a convergence study is genuinely neither
    a build nor a run, but adding a verb per kind would push the taxonomy into the
    protocol and force every backend to grow with it.
    """

    kind: JobKind
    slug: str
    solver: str
    title: str
    capability: Capability | None
    verb: str                             # build | run | study
    options: type
    progress: type
    workspace_default: str                # job | case
    needs_environment: bool


#: Build kinds default to ``workspace: job`` (they produce their own inputs, so a
#: self-contained directory is right). Run kinds default to ``workspace: case``, because
#: they hotstart from a result that already sits in the case's ``model_dir`` and copying
#: it would violate the large-data rule (plan §26).
KIND_META: dict[JobKind, KindMeta] = {
    JobKind.PREPROCESSING: KindMeta(
        JobKind.PREPROCESSING, "preproc", "telemac", "Build the TELEMAC case",
        Capability.STEADY2D, "build", PreprocessingOptions, PreprocessingProgress,
        "job", False),
    JobKind.STEADY_RUN: KindMeta(
        JobKind.STEADY_RUN, "steady", "telemac", "Steady 2D run",
        Capability.STEADY2D, "run", SteadyRunOptions, SolverRunProgress, "case", True),
    JobKind.MESH_CONVERGENCE: KindMeta(
        JobKind.MESH_CONVERGENCE, "meshconv", "telemac", "Mesh-convergence study",
        Capability.MESH_CONVERGENCE, "study", MeshConvergenceOptions, LadderProgress,
        "case", True),
    JobKind.BUILD_3D: KindMeta(
        JobKind.BUILD_3D, "build3d", "telemac", "Write the TELEMAC-3D steering files",
        Capability.STEADY3D, "build", Build3DOptions, PreprocessingProgress, "case",
        False),
    JobKind.STEADY_RUN_3D: KindMeta(
        JobKind.STEADY_RUN_3D, "steady3d", "telemac", "Steady 3D run",
        Capability.STEADY3D, "run", Steady3DOptions, SolverRunProgress, "case", True),
    JobKind.VERTICAL_CONVERGENCE: KindMeta(
        JobKind.VERTICAL_CONVERGENCE, "vertconv", "telemac",
        "Vertical-layer convergence study", Capability.VERTICAL_CONVERGENCE, "study",
        VerticalConvergenceOptions, LadderProgress, "case", True),
    JobKind.UNSTEADY_RUN: KindMeta(
        JobKind.UNSTEADY_RUN, "unsteady", "telemac", "Unsteady (hydrograph) run",
        Capability.UNSTEADY2D, "run", UnsteadyOptions, SolverRunProgress, "case", True),
    JobKind.OPENFOAM_BUILD: KindMeta(
        JobKind.OPENFOAM_BUILD, "ofbuild", "openfoam", "Build the OpenFOAM case",
        Capability.FREE_SURFACE_3D, "build", OpenFoamBuildOptions, OpenFoamBuildProgress,
        "case", False),
    JobKind.OPENFOAM_RUN: KindMeta(
        JobKind.OPENFOAM_RUN, "ofrun", "openfoam", "OpenFOAM interFoam run",
        Capability.FREE_SURFACE_3D, "run", OpenFoamRunOptions, SolverRunProgress,
        "case", True),
    JobKind.CALIBRATION: KindMeta(
        JobKind.CALIBRATION, "bal", "telemac", "HydroBayesCal calibration",
        Capability.CALIBRATION, "study", CalibrationOptions, CalibrationProgress,
        "case", True),
    JobKind.CALIBRATION_MULTIFLOW: KindMeta(
        JobKind.CALIBRATION_MULTIFLOW, "balmf", "telemac",
        "HydroBayesCal multi-flow calibration", Capability.CALIBRATION, "study",
        MultiflowCalibrationOptions, CalibrationProgress, "case", True),
}


def kinds_for(solver: str) -> list[JobKind]:
    return [k for k, m in KIND_META.items() if m.solver == solver]


# --------------------------------------------------------------------------- records


@dataclass
class Resources:
    mpi_processes: int = 1
    working_root: str | None = None
    time_limit_s: float | None = None
    #: Refuse to start below this much free space (plan §24). 5 GiB is a floor, not a
    #: forecast - a real OpenFOAM run wants far more, but this catches the full volume.
    min_free_bytes: int = 5 * 1024 ** 3

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Resources":
        return cls(**{k: v for k, v in dict(data or {}).items()
                      if k in {f.name for f in fields(cls)}})

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class Workspace:
    """Where the job's working directories point.

    ``job``
        Rebase the case's four phase directories into ``<job>/input`` and
        ``<job>/results``. Right for a build, which produces its own inputs.
    ``case``
        Leave them alone. Right for a run that hotstarts from an ``r2d.slf`` already
        sitting in the case's ``model_dir`` - and the only mode under which the
        standalone scripts and the job system operate on the same files.
    ``link``
        Point at a *previous job's* build. Copies nothing (plan §26).
    """

    mode: str = "case"
    source_job: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Workspace":
        data = dict(data or {})
        mode = str(data.get("mode", "case"))
        if mode not in {"job", "case", "link"}:
            raise ConfigError(f"unknown workspace mode {mode!r}", subject="workspace.mode",
                              remedy="Use one of: job, case, link")
        source = data.get("source_job")
        if mode == "link" and not source:
            raise ConfigError("workspace mode 'link' needs a source_job",
                              subject="workspace.source_job")
        return cls(mode=mode, source_job=source)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "source_job": self.source_job}


@dataclass
class JobSpec:
    """``job.json``: what was asked for, frozen."""

    job_id: str
    kind: JobKind
    solver: str
    schema_version: int = JOB_SCHEMA_VERSION
    solver_profile: str | None = None
    created: str = field(default_factory=utc_now)
    created_by: dict[str, Any] = field(default_factory=dict)
    case: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    resources: Resources = field(default_factory=Resources)
    workspace: Workspace = field(default_factory=Workspace)
    options: Options = field(default_factory=Options)
    launcher: dict[str, Any] = field(default_factory=lambda: {"requested": "auto"})

    # -- convenience ------------------------------------------------------------
    @property
    def meta(self) -> KindMeta:
        return KIND_META[self.kind]

    @property
    def case_name(self) -> str:
        return str(self.case.get("name", "case"))

    @property
    def source_config(self) -> str | None:
        value = self.case.get("source_config")
        return str(value) if value else None

    # -- serialisation ----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "kind": self.kind.value,
            "solver": self.solver,
            "solver_profile": self.solver_profile,
            "created": self.created,
            "created_by": dict(self.created_by),
            "case": dict(self.case),
            "workspace": self.workspace.as_dict(),
            "environment": dict(self.environment),
            "resources": self.resources.as_dict(),
            "options": self.options.as_dict(),
            "launcher": dict(self.launcher),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobSpec":
        known = {"schema_version", "job_id", "kind", "solver", "solver_profile",
                 "created", "created_by", "case", "workspace", "environment",
                 "resources", "options", "launcher"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                "job.json carries unknown field" + ("s" if len(unknown) > 1 else "")
                + ": " + ", ".join(unknown),
                subject=unknown[0],
                remedy="This job may have been written by a newer hydromate. "
                       "Check 'schema_version'.",
            )
        kind = parse_kind(data["kind"])
        meta = KIND_META[kind]
        return cls(
            job_id=str(data["job_id"]),
            kind=kind,
            solver=str(data.get("solver") or meta.solver),
            schema_version=int(data.get("schema_version", JOB_SCHEMA_VERSION)),
            solver_profile=data.get("solver_profile"),
            created=str(data.get("created") or utc_now()),
            created_by=dict(data.get("created_by") or {}),
            case=dict(data.get("case") or {}),
            environment=dict(data.get("environment") or {}),
            resources=Resources.from_dict(data.get("resources")),
            workspace=Workspace.from_dict(data.get("workspace")),
            options=meta.options.from_dict(data.get("options")),
            launcher=dict(data.get("launcher") or {"requested": "auto"}),
        )


@dataclass
class JobStatus:
    """``status.json``: where the job is now.

    Only a process holding ``job.lock`` writes this. That is the rule that makes plan
    §3's "exactly one writer per file" testable rather than aspirational.
    """

    job_id: str
    state: JobState = JobState.QUEUED
    kind: JobKind | None = None
    solver: str = ""
    schema_version: int = STATUS_SCHEMA_VERSION
    phase: str = ""
    progress: Any = None
    launcher: dict[str, Any] | None = None
    started: str | None = None
    updated: str = field(default_factory=utc_now)
    finished: str | None = None
    exit_code: int | None = None
    error: dict[str, Any] | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)

    def transition(self, target: JobState, *, when: str | None = None) -> "JobStatus":
        """Move to *target*, recording it. Raises on an illegal edge."""
        check_transition(self.state, target)
        stamp = when or utc_now()
        if target is not self.state:
            self.history.append({"state": target.value, "at": stamp})
        self.state = target
        self.updated = stamp
        if target is JobState.STARTING and not self.started:
            self.started = stamp
        if target.is_terminal:
            self.finished = stamp
        return self

    def as_dict(self) -> dict[str, Any]:
        progress = self.progress
        if isinstance(progress, Progress):
            progress = progress.as_dict()
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "state": self.state.value,
            "kind": self.kind.value if self.kind else None,
            "solver": self.solver,
            "phase": self.phase,
            "progress": progress,
            "launcher": self.launcher,
            "started": self.started,
            "updated": self.updated,
            "finished": self.finished,
            "exit_code": self.exit_code,
            "error": self.error,
            "artifacts": dict(self.artifacts),
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobStatus":
        kind = data.get("kind")
        return cls(
            job_id=str(data.get("job_id", "")),
            state=JobState(str(data.get("state", "QUEUED"))),
            kind=parse_kind(kind) if kind else None,
            solver=str(data.get("solver") or ""),
            schema_version=int(data.get("schema_version", STATUS_SCHEMA_VERSION)),
            phase=str(data.get("phase") or ""),
            progress=Progress.from_dict(data.get("progress")),
            launcher=data.get("launcher"),
            started=data.get("started"),
            updated=str(data.get("updated") or utc_now()),
            finished=data.get("finished"),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
            artifacts=dict(data.get("artifacts") or {}),
            history=list(data.get("history") or []),
        )

    @classmethod
    def initial(cls, spec: JobSpec) -> "JobStatus":
        return cls(job_id=spec.job_id, kind=spec.kind, solver=spec.solver,
                   state=JobState.QUEUED,
                   progress=spec.meta.progress(),
                   history=[{"state": JobState.QUEUED.value, "at": utc_now()}])
