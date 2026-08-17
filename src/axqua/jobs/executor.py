"""Running one job: dispatch, state, and turning any failure into a record.

This is the only module in :mod:`axqua.jobs` that touches a solver, and it touches
it through exactly one seam - ``registry.get(solver).load()``. There is no
``if solver == "telemac"`` here and there must never be (plan §12): the kind table says
which *verb* to call, the backend decides what that verb means, and adding a third solver
is a `pip install`, not an edit to this file.

The shape of a run::

    lock -> runner.log -> load the frozen config -> rebase the workspace
         -> disk check -> STARTING -> RUNNING -> backend.<verb>(...)
         -> POSTPROCESSING -> backend.postprocess / export_qgis_results
         -> results.json -> COMPLETED

Every failure becomes a :class:`~axqua.core.errors.ErrorRecord` in ``status.json``
*and* an ``ERROR`` line in ``runner.log``. Plan §24 requires both, and the reason is
practical: the record is what a GUI can act on, the log is what a human can read.

Cancellation is cooperative first. ``cancel`` writes a request file; the token below is
checked between steps and inside the streaming callback, so the job tears itself down and
writes ``CANCELLED`` on its own. The launcher's tree-kill is the fallback for a job that
is too busy or too stuck to notice.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axqua.core import errors, registry
from axqua.core.capabilities import Capability
from axqua.core.errors import ErrorRecord
from axqua.jobs import events, logs, results
from axqua.jobs.interaction import PolicyAsk
from axqua.jobs.lock import JobLock
from axqua.jobs.model import (KIND_META, JobKind, JobSpec, JobState, JobStatus,
                                  utc_now)
from axqua.jobs.paths import JobDir
from axqua.jobs.store import read_spec, read_status, write_status

log = logging.getLogger("axqua.jobs.executor")

__all__ = ["CancelToken", "ExecutionContext", "JobCancelled", "execute"]

#: How often the cancel token re-reads the request file. A stat per second is free and
#: makes cancellation feel immediate without polling anything expensive.
CANCEL_POLL_INTERVAL = 1.0


class JobCancelled(Exception):
    """Raised inside the job when a cancellation request is noticed."""


class CancelToken:
    """Cooperative cancellation, driven by a file.

    A file rather than a signal because the request comes from a *different process*
    (possibly a different QGIS session, possibly days later), and because it preserves
    the one-writer-per-file rule: ``cancel`` never touches ``status.json``.
    """

    def __init__(self, job_dir: JobDir, *, interval: float = CANCEL_POLL_INTERVAL) -> None:
        self.job_dir = job_dir
        self.interval = float(interval)
        self._last = 0.0
        self._set = False

    @property
    def requested(self) -> bool:
        if self._set:
            return True
        now = time.monotonic()
        if (now - self._last) < self.interval:
            return False
        self._last = now
        self._set = self.job_dir.cancel_request.exists()
        return self._set

    def check(self) -> None:
        if self.requested:
            raise JobCancelled("cancellation requested")

    def __call__(self) -> bool:
        """Usable directly as a ``should_stop`` callback."""
        return self.requested


@dataclass
class ExecutionContext:
    """Everything a backend needs that is not the case configuration.

    Passed as ``ctx=`` rather than as loose keyword arguments so a backend can grow a new
    need without every caller changing, and so the standalone case scripts - which pass
    no context at all - keep working against the same backend methods.
    """

    job_dir: JobDir
    spec: JobSpec
    cfg: Any
    sink: Any = field(default_factory=events.NullSink)
    cancel: CancelToken | None = None
    manifest: results.ResultManifest | None = None
    log: logging.Logger = log
    _ask: Any = None
    _open_sinks: list[Any] = field(default_factory=list)

    # -- helpers a backend calls --------------------------------------------------
    @property
    def capability(self) -> Capability | None:
        return self.spec.meta.capability

    @property
    def options(self) -> Any:
        return self.spec.options

    def should_stop(self) -> bool:
        return bool(self.cancel and self.cancel.requested)

    def check_cancelled(self) -> None:
        if self.cancel:
            self.cancel.check()

    def ask(self, prompt: str, *, default: bool = False, timeout: float | None = None
            ) -> bool:
        """Answer a study's question from the job record. Never reads stdin."""
        if self._ask is None:
            answers = getattr(self.spec.options, "answers", None)
            self._ask = PolicyAsk(answers, sink=self.sink,
                                  answer_file=self.job_dir.answers)
        return self._ask(prompt, default=default, timeout=timeout)

    def progress(self, **fields: Any) -> None:
        self.sink.progress(**fields)

    def solver_sink(self, name: str) -> Any:
        """A rotating text sink for one solver invocation's raw output."""
        sink = logs.RotatingTextSink(self.job_dir.solver_logs / f"{name}.log")
        self._open_sinks.append(sink)
        return sink

    def record(self, name: str, path: str | os.PathLike, **kwargs: Any) -> None:
        """Add an artifact to the result manifest."""
        if self.manifest is not None:
            self.manifest.add(name, path, base=self.job_dir.root, **kwargs)

    def close(self) -> None:
        for sink in self._open_sinks:
            try:
                sink.close()
            except Exception:                       # noqa: BLE001 - pragma: no cover
                pass
        self._open_sinks.clear()


# ------------------------------------------------------------------------ workspace


def rebase(cfg: Any, job_dir: JobDir) -> None:
    """Point the case's four phase directories inside the job.

    Four assignments and nothing else, because every path in axqua already resolves
    through ``cfg.model_path()`` and its siblings - which is exactly why the plan's
    "job-oriented working directories" is a small change rather than a rewrite.
    """
    cfg.preprocessing_dir = job_dir.preprocessing
    cfg.model_dir = job_dir.simulation
    cfg.postprocessing_dir = job_dir.results
    cfg.calibration_dir = job_dir.calibration
    cfg.ensure_dirs()


def apply_workspace(cfg: Any, spec: JobSpec, job_dir: JobDir) -> None:
    mode = spec.workspace.mode
    if mode == "job":
        rebase(cfg, job_dir)
        return
    if mode == "link":
        source = JobDir(job_dir.root.parent / str(spec.workspace.source_job))
        if not source.exists():
            raise errors.ConfigError(
                f"job {spec.workspace.source_job} was named as this job's workspace, "
                "but it is not there",
                subject="workspace.source_job",
                remedy="Run 'axqua list' to see the jobs that exist.",
            )
        # Point at the parent's build without copying a byte of it (plan §26).
        cfg.model_dir = source.simulation
        cfg.preprocessing_dir = source.preprocessing
        cfg.postprocessing_dir = job_dir.results
        cfg.calibration_dir = job_dir.calibration
        cfg.ensure_dirs()
        return
    # mode "case": leave the case's own directories alone. This is the only mode under
    # which a run can hotstart from an r2d.slf the standalone scripts produced, and it
    # is why run kinds default to it.
    cfg.ensure_dirs()


# ------------------------------------------------------------------------- dispatch


def execute(job_dir: JobDir | str | os.PathLike, *, sink: Any = None,
            console: bool = False, break_stale_lock: bool = True) -> int:
    """Run the job in *job_dir* to completion. Returns a process exit code.

    Synchronous by design - this is what ``axqua execute`` calls, and what every
    detached launcher ultimately starts. Keeping the detached path and the debugging
    path identical means a failure reproduces under a terminal.
    """
    jd = job_dir if isinstance(job_dir, JobDir) else JobDir(job_dir)
    lock = JobLock(jd)
    stale, _reason = lock.is_stale()
    with lock.holding(purpose="execute", break_stale=break_stale_lock and stale):
        # *console* is off by default: the CLI has already attached a console handler,
        # and adding a second one makes every line appear twice - which, for a detached
        # job whose stdout is redirected to a file, doubles the size of that file too.
        with logs.runner_log(jd.runner_log, console=console):
            return _run(jd, sink=sink)


def _run(jd: JobDir, *, sink: Any = None) -> int:
    from axqua.config import load_config

    spec = read_spec(jd)
    status = read_status(jd) or JobStatus.initial(spec)
    status.launcher = status.launcher or {}
    meta = spec.meta

    status_sink = events.StatusFileSink(jd, status)
    sinks = [status_sink]
    if sink is not None:
        sinks.append(sink)
    combined = events.TeeSink(*sinks)

    manifest = results.ResultManifest(job_id=spec.job_id, solver=spec.solver,
                                      kind=spec.kind.value)
    cancel = CancelToken(jd)
    ctx: ExecutionContext | None = None

    log.info("job %s: %s (%s)", spec.job_id, meta.title, spec.solver)
    with events.using_sink(combined):
        try:
            if cancel.requested:
                # Cancelled before it ever really started: there is nothing to tear down.
                _finish(jd, status, JobState.CANCELLED, status_sink)
                return 0

            cfg = load_config(jd.frozen_config)
            apply_workspace(cfg, spec, jd)
            logs.require_free_space(jd.root, spec.resources.min_free_bytes)

            ctx = ExecutionContext(job_dir=jd, spec=spec, cfg=cfg, sink=combined,
                                   cancel=cancel, manifest=manifest, log=log)

            status.transition(JobState.STARTING)
            status_sink.flush()

            backend = _load_backend(spec)
            if meta.needs_environment:
                _check_environment(backend, cfg, spec)

            status.transition(JobState.RUNNING)
            status_sink.flush()

            verb = getattr(backend, meta.verb, None)
            if verb is None:
                raise errors.SolverError(
                    f"the {spec.solver} backend cannot {meta.verb} "
                    f"{meta.capability.value if meta.capability else spec.kind.value}",
                    subject="kind",
                    component=spec.solver,
                )
            log.info("running %s.%s for %s", spec.solver, meta.verb, spec.kind.value)
            kwargs = dict(spec.options.as_kwargs())
            kwargs.update(_pre_step(spec, cfg, ctx))
            artifacts = verb(cfg, meta.capability, ctx=ctx, **kwargs)
            cancel.check()

            status.transition(JobState.POSTPROCESSING)
            status_sink.flush()
            _postprocess(backend, cfg, ctx, manifest, artifacts)

            results.write_manifest(jd, manifest)
            status.artifacts.update(_artifact_map(jd))
            _finish(jd, status, JobState.COMPLETED, status_sink, exit_code=0)
            log.info("job %s COMPLETED", spec.job_id)
            return 0

        except JobCancelled:
            log.warning("job %s cancelled", spec.job_id)
            if status.state is not JobState.CANCEL_REQUESTED:
                status.transition(JobState.CANCEL_REQUESTED)
            _finish(jd, status, JobState.CANCELLED, status_sink)
            return 130                      # the conventional "terminated by request"

        except BaseException as exc:        # noqa: BLE001 - every failure is recorded
            record = _classify(exc, spec)
            # Both, always: the record is what a GUI can act on, the log is what a
            # person can read (plan §24).
            log.error("job %s FAILED [%s]: %s", spec.job_id, record.code, record.message)
            if record.remedy:
                log.error("  remedy: %s", record.remedy)
            log.debug("traceback", exc_info=True)
            status.error = record.as_dict()
            status.artifacts.update(_artifact_map(jd))
            _finish(jd, status, JobState.FAILED, status_sink,
                    exit_code=int(record.details.get("return_code") or 1))
            if isinstance(exc, KeyboardInterrupt):
                raise
            return 1
        finally:
            if ctx is not None:
                ctx.close()
            status_sink.close()


def _pre_step(spec: JobSpec, cfg: Any, ctx: ExecutionContext) -> dict[str, Any]:
    """Cross-backend orchestration that must not live inside either backend.

    Only one case so far, and it is instructive: an OpenFOAM build is seeded from a
    converged **TELEMAC** result. Obtaining that seed from inside the OpenFOAM backend
    would make one backend reach into its sibling - forbidden by
    ``tests/test_capabilities.py``, and wrong regardless. So it happens here, above both,
    which is exactly where ``prerun.py`` already documents that such orchestration
    belongs.
    """
    if spec.kind is not JobKind.OPENFOAM_BUILD:
        return {}
    from axqua import prerun
    enabled = getattr(spec.options, "pre_run", None)
    try:
        seed = prerun.ensure_seed(cfg, enabled=enabled)
    except Exception as exc:                        # noqa: BLE001
        # A missing seed costs accuracy, not the build: the case still meshes under a
        # flat lid, which is the documented --no-pre-run behaviour.
        log.warning("no 2D seed for the OpenFOAM build (%s: %s); building cold",
                    type(exc).__name__, exc)
        return {}
    for line in seed.summary():
        log.info("seed: %s", line)
    ctx.sink.step("seed", done=True)
    return {"seed": seed.path} if getattr(seed, "path", None) else {}


def _finish(jd: JobDir, status: JobStatus, target: JobState, sink: Any, *,
            exit_code: int | None = None) -> None:
    """Record the terminal state, unconditionally.

    Written through ``write_status`` rather than through the sink on purpose. The sink
    throttles and only writes when it believes something changed, and the fields set here
    - ``error``, ``exit_code``, the final transition - are assigned on the status object
    directly, so the sink would consider itself clean and skip the write. The job would
    then finish with the runner log carrying the real failure while ``status.json`` still
    said RUNNING, and the reaper would later relabel it as abandoned - losing the actual
    error. This is the one write that must never be skipped.
    """
    try:
        status.transition(target)
    except errors.AxquaError:
        # A terminal state we cannot legally reach means the job already settled, which
        # is not worth failing over at the very end of a run.
        log.debug("could not record final state %s from %s", target, status.state)
    if exit_code is not None:
        status.exit_code = exit_code
    status.updated = utc_now()
    try:
        write_status(jd, status)
    except Exception:                               # noqa: BLE001
        log.exception("could not write the final status for %s", status.job_id)
    finally:
        # Keep the sink from re-writing a now-stale copy over what we just wrote.
        setattr(sink, "_dirty", False)


def _load_backend(spec: JobSpec):
    try:
        return registry.get(spec.solver).load()
    except KeyError as exc:
        raise errors.ConfigError(
            f"no solver backend named {spec.solver!r}",
            subject="solver",
            remedy="Registered solvers: " + ", ".join(s.name for s in registry.backends()),
        ) from exc
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise errors.EnvironmentError(
            f"the {spec.solver} backend could not be loaded",
            subject="solver",
            remedy="Reinstall axqua, or check the plugin providing this backend.",
            cause=str(exc),
        ) from exc


def _check_environment(backend: Any, cfg: Any, spec: JobSpec) -> None:
    """Fail before the solver, not during it."""
    try:
        ok = backend.check_environment(cfg)
    except Exception as exc:                        # noqa: BLE001
        raise errors.EnvironmentError(
            f"the {spec.solver} environment could not be checked",
            subject="telemac.pysource" if spec.solver == "telemac" else "openfoam.bashrc",
            remedy="Run 'axqua profiles validate' to see what is missing.",
            cause=str(exc),
        ) from exc
    if ok is False:
        raise errors.EnvironmentError(
            f"the {spec.solver} environment is not available on this machine",
            subject="telemac.pysource" if spec.solver == "telemac" else "openfoam.bashrc",
            remedy="Check the setup script in the solver profile, then run "
                   "'axqua profiles validate'.",
        )


def _postprocess(backend: Any, cfg: Any, ctx: ExecutionContext,
                 manifest: results.ResultManifest, artifacts: Any) -> None:
    """Postprocess, extract, export - each guarded separately.

    A failure here must not discard a run that already succeeded: hours of solver time
    are worth more than a missing figure, so each step is reported and the job still
    completes. A failing *solver* is a different matter and is not caught here.
    """
    for name, call in (
        ("postprocess", lambda: backend.postprocess(cfg, ctx)),
        ("export_qgis_results", lambda: backend.export_qgis_results(cfg, ctx)),
    ):
        try:
            call()
        except Exception as exc:                    # noqa: BLE001
            log.warning("%s failed (the run itself succeeded): %s: %s",
                        name, type(exc).__name__, exc)
            manifest.summary.setdefault("warnings", []).append(f"{name}: {exc}")
    try:
        manifest.objective = backend.extract_objective(cfg, ctx)
    except Exception as exc:                        # noqa: BLE001
        log.warning("objective extraction failed: %s: %s", type(exc).__name__, exc)
    if artifacts is not None and not manifest.summary.get("artifacts"):
        manifest.summary["artifacts"] = _describe(artifacts)


def _describe(artifacts: Any) -> Any:
    """Summarise whatever a backend verb returned, without serialising bulk data."""
    if artifacts is None:
        return None
    if isinstance(artifacts, (str, int, float, bool)):
        return artifacts
    if isinstance(artifacts, Path):
        return str(artifacts)
    if isinstance(artifacts, dict):
        return {str(k): _describe(v) for k, v in list(artifacts.items())[:50]}
    if isinstance(artifacts, (list, tuple, set)):
        return [_describe(v) for v in list(artifacts)[:50]]
    if hasattr(artifacts, "__dataclass_fields__"):
        return {k: _describe(getattr(artifacts, k, None))
                for k in artifacts.__dataclass_fields__}
    return type(artifacts).__name__


def _artifact_map(jd: JobDir) -> dict[str, str]:
    out = {"runner_log": str(jd.runner_log.relative_to(jd.root))}
    if jd.results_json.exists():
        out["results_json"] = str(jd.results_json.relative_to(jd.root))
    if jd.qgis.is_dir():
        out["qgis"] = str(jd.qgis.relative_to(jd.root))
    listings = sorted(jd.solver_logs.glob("*.log")) if jd.solver_logs.is_dir() else []
    if listings:
        out["solver_log"] = str(listings[-1].relative_to(jd.root))
    return out


#: How an exception becomes a stable code. The mapping is a table rather than a chain of
#: ``isinstance`` checks so plan §24's failure list is visibly covered.
_FAILURE_HINTS: tuple[tuple[type[BaseException], str, str], ...] = (
    (FileNotFoundError, "axqua.config",
     "A file the job needs is missing. Check the paths in the frozen "
     "input/case-config.yml."),
    (PermissionError, "axqua.environment",
     "axqua could not read or write a path the job needs."),
    (MemoryError, "axqua.environment",
     "The machine ran out of memory. Reduce the mesh resolution or the process count."),
)


def _classify(exc: BaseException, spec: JobSpec) -> ErrorRecord:
    if isinstance(exc, errors.AxquaError):
        record = ErrorRecord.from_exception(exc)
        record.details.setdefault("component", spec.solver)
        return record
    for exc_type, code, remedy in _FAILURE_HINTS:
        if isinstance(exc, exc_type):
            return ErrorRecord(code=code, message=f"{type(exc).__name__}: {exc}",
                               remedy=remedy,
                               details={"component": spec.solver,
                                        "type": type(exc).__name__})
    record = ErrorRecord.from_exception(exc)
    record.details.setdefault("component", spec.solver)
    return record


def capability_for(kind: JobKind) -> Capability | None:
    return KIND_META[kind].capability


def verb_for(kind: JobKind) -> str:
    return KIND_META[kind].verb


# Re-exported so callers do not have to reach into ``model`` for the dispatch table.
Dispatch = Callable[..., Any]
