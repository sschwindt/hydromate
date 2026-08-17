"""The TELEMAC backend: what the job system calls, adapting what already exists.

Deliberately thin. Every piece of work here is a function the case-template scripts have
been calling for months - ``pipeline.run``, ``run_solver_streaming``,
``analyze_flux_convergence``, ``run_mesh_convergence``, ``run_single_flow_calibration``.
The backend adds no hydraulics and no numerics; it maps a *verb plus a capability* onto
the right one of them and passes the job's event sink and cancel token down.

That thinness is the point. ``cases/*/initial_run.py`` and a submitted ``steady`` job must
drive the same code, because the standalone scripts remain the supported path (plan §29)
and a second implementation would drift the moment one of them was fixed.

This module is loaded **only** through ``BackendSpec.load()``. It imports gmsh, numpy and
the rest transitively, so it must never be imported by ``spec.py`` or by the package
``__init__`` - ``tests/test_capabilities.py`` asserts exactly that from a subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from axqua.core import errors
from axqua.core.capabilities import Capability
from axqua.core.registry import BaseBackend
from axqua.jobs.results import STYLE_DEPTH, STYLE_VELOCITY

log = logging.getLogger("axqua.solvers.telemac.backend")

#: Which ``.cas`` each 3D variant runs, and what its result is called. Kept here rather
#: than re-derived, because ``spec.py`` already names the same three files and the two
#: must not drift.
_3D_VARIANTS = {
    "hydrostatic": ("hotstart3d_hydrostatic.cas", "r3d-hydrostatic.slf"),
    "hydrodyn": ("hotstart3d_hydrodyn.cas", None),
    "unsteady": ("unsteady3d.cas", None),
}


class TelemacBackend(BaseBackend):
    """TELEMAC-2D / TELEMAC-3D, with GAIA where the case asks for it."""

    name = "telemac"

    # -- environment --------------------------------------------------------------
    def check_environment(self, cfg) -> bool:
        """True when the TELEMAC environment can actually be entered.

        Must not raise (the protocol says so), because this is also called from
        ``axqua status --check-env`` where a broken install is information, not a
        crash.
        """
        from axqua.env import TelemacRuntime
        try:
            TelemacRuntime(cfg.telemac).check_available()
        except Exception as exc:  # noqa: BLE001 - report, never raise
            log.debug("telemac unavailable: %s: %s", type(exc).__name__, exc)
            return False
        return True

    # -- build --------------------------------------------------------------------
    def build(self, cfg, capability: Capability | None = None, *, ctx: Any = None,
              **kwargs):
        capability = capability or Capability.STEADY2D
        if capability in (Capability.UNSTEADY2D, Capability.UNSTEADY3D):
            return self._build_unsteady(cfg, ctx=ctx, **kwargs)
        if capability in (Capability.STEADY3D,):
            return self._build_3d(cfg, ctx=ctx, **kwargs)
        return self._build_case(cfg, ctx=ctx, **kwargs)

    def _build_case(self, cfg, *, ctx: Any = None, discharge: float | None = None,
                    validate_env: bool = True, dry_run: bool = False, **_ignored):
        from axqua.solvers.telemac import pipeline
        from axqua.workflow import prepare_steady_inputs

        prepare_steady_inputs(cfg, discharge)
        if ctx is not None:
            ctx.check_cancelled()
        # log_to_file=False: the job already owns runner.log, and a second logfile in
        # model_dir would split the narration of one run across two places.
        artifacts = pipeline.run(cfg, validate_env=validate_env, dry_run=dry_run,
                                 log_to_file=False)
        if ctx is not None:
            for name in ("geometry_slf", "boundary_cli", "cas_file", "friction_tbl"):
                path = getattr(artifacts, name, None)
                if path:
                    ctx.record(Path(path).name, path, kind="file")
        return artifacts

    def _build_3d(self, cfg, *, ctx: Any = None, hydrostatic_steps: int | None = None,
                  n_levels: int | None = None, **_ignored):
        from axqua.solvers.telemac.threed import build_3d_cases
        kwargs: dict[str, Any] = {}
        if hydrostatic_steps is not None:
            kwargs["hydrostatic_steps"] = hydrostatic_steps
        if n_levels is not None:
            kwargs["n_levels"] = n_levels
        return build_3d_cases(cfg, **kwargs)

    def _build_unsteady(self, cfg, *, ctx: Any = None, mode_3d: bool = False,
                        control_sections: bool = True, **_ignored):
        from axqua.solvers.telemac.unsteady import (build_unsteady_3d_case,
                                                        build_unsteady_case)
        if mode_3d:
            return build_unsteady_3d_case(cfg)
        return build_unsteady_case(cfg, control_sections=control_sections)

    # -- run ----------------------------------------------------------------------
    def run(self, cfg, capability: Capability | None = None, *, ctx: Any = None,
            **kwargs):
        capability = capability or Capability.STEADY2D
        if capability is Capability.STEADY3D:
            return self._run_3d(cfg, ctx=ctx, **kwargs)
        if capability in (Capability.UNSTEADY2D, Capability.UNSTEADY3D):
            return self._run_unsteady(cfg, ctx=ctx, **kwargs)
        return self._run_steady(cfg, ctx=ctx, **kwargs)

    def _run_steady(self, cfg, *, ctx: Any = None, ncsize: int | None = None,
                    cas_file: str | None = None, **_ignored):
        return self._launch(cfg, ctx=ctx, cas_file=cas_file or cfg.cas_file,
                            ncsize=ncsize, solver=None, name="telemac2d")

    def _run_3d(self, cfg, *, ctx: Any = None, variant: str = "hydrostatic",
                ncsize: int | None = None, **_ignored):
        if variant not in _3D_VARIANTS:
            raise errors.ConfigError(
                f"unknown 3D variant {variant!r}",
                subject="options.variant",
                remedy="Use one of: " + ", ".join(_3D_VARIANTS),
            )
        cas_name = _3D_VARIANTS[variant][0]
        if not cfg.model_path(cas_name).exists():
            raise errors.ConfigError(
                f"{cas_name} has not been written yet",
                subject="options.variant",
                remedy="Submit a build-3d job first (or run add3d.py).",
            )
        # TELEMAC-3D has no DURATION keyword, so the progress bar spans the fixed step
        # count instead - which is why the duration is computed rather than read.
        duration = self._three_d_duration(cfg, cas_name)
        return self._launch(cfg, ctx=ctx, cas_file=cas_name, ncsize=ncsize,
                            solver="telemac3d", name=f"telemac3d-{variant}",
                            duration=duration)

    def _run_unsteady(self, cfg, *, ctx: Any = None, mode_3d: bool = False,
                      ncsize: int | None = None, run: bool = True, **_ignored):
        if not run:
            return None
        cas = (cfg.results3d_unsteady_slf and "unsteady3d.cas") if mode_3d else \
            cfg.unsteady_cas_file
        return self._launch(cfg, ctx=ctx, cas_file=cas, ncsize=ncsize,
                            solver="telemac3d" if mode_3d else None,
                            name="telemac-unsteady")

    def _launch(self, cfg, *, ctx: Any, cas_file: str, ncsize: int | None,
                solver: str | None, name: str, duration: float | None = None):
        from axqua.env import TelemacRuntime
        from axqua.workflow import run_solver_streaming

        cas = cfg.model_path(cas_file)
        if not cas.exists():
            raise errors.ConfigError(
                f"no built case at {cas}",
                subject="model_dir",
                remedy="Submit a preprocessing job first, or point the job at the case "
                       "workspace that holds the build.",
            )
        runtime = TelemacRuntime(cfg.telemac)
        sink = _sink_for(ctx, name)
        proc = run_solver_streaming(
            runtime, cfg, cas_file=cas_file, solver=solver, ncsize=ncsize,
            duration=duration,
            # A detached job has no terminal to draw a bar on, and echoing the listing
            # to a dead stdout is pure waste; the sink still receives every line.
            show_progress=ctx is None,
            sink=sink,
            should_stop=ctx.should_stop if ctx is not None else None,
        )
        if ctx is not None:
            ctx.check_cancelled()
        if proc.returncode != 0:
            raise errors.SolverError(
                f"{solver or cfg.telemac.solver} exited with code {proc.returncode}",
                subject=str(cas),
                remedy=f"Read the listing next to {cas.parent} for the first error.",
                component=solver or cfg.telemac.solver,
                return_code=proc.returncode,
                listing=str(cas.with_suffix(".sortie")),
            )
        return proc

    @staticmethod
    def _three_d_duration(cfg, cas_name: str) -> float | None:
        """Read TIME STEP x NUMBER OF TIME STEPS out of a 3D steering file.

        TELEMAC-3D genuinely has no ``DURATION``, so the only honest span for a progress
        bar is the product of the two keywords the file does carry.
        """
        try:
            text = cfg.model_path(cas_name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        step = steps = None
        for line in text.splitlines():
            if ":" not in line or line.lstrip().startswith("/"):
                continue
            key, _, value = line.partition(":")
            key = key.strip().upper()
            try:
                if key == "TIME STEP":
                    step = float(value.strip())
                elif key == "NUMBER OF TIME STEPS":
                    steps = int(float(value.strip()))
            except ValueError:
                continue
        return step * steps if (step and steps) else None

    # -- study --------------------------------------------------------------------
    def study(self, cfg, capability: Capability | None = None, *, ctx: Any = None,
              **kwargs):
        if capability is Capability.MESH_CONVERGENCE:
            return self._mesh_convergence(cfg, ctx=ctx, **kwargs)
        if capability is Capability.VERTICAL_CONVERGENCE:
            return self._vertical_convergence(cfg, ctx=ctx, **kwargs)
        if capability is Capability.CALIBRATION:
            return self._calibration(cfg, ctx=ctx, **kwargs)
        raise errors.ConfigError(
            f"telemac has no study for {capability}",
            subject="kind",
        )

    def _mesh_convergence(self, cfg, *, ctx: Any = None, discharge: float | None = None,
                          refinement_ratio: float = 1.3, tolerance: float = 0.05,
                          n_coarser: int = 1, n_finer: int = 2,
                          max_extra_levels: int = 3, auto_extend: bool = False,
                          initial_depth: float | None = 0.5, ncsize: int | None = None,
                          answers: dict | None = None, **_ignored):
        from axqua.convergence import ratio_levels, run_mesh_convergence
        from axqua.workflow import prepare_steady_inputs

        q = prepare_steady_inputs(cfg, discharge, prewet_depth=initial_depth)
        levels = ratio_levels(cfg, ratio=refinement_ratio, n_coarser=n_coarser,
                              n_finer=n_finer)
        base = Path(cfg.postprocessing_dir) / "mesh-convergence"
        report = run_mesh_convergence(
            cfg, discharge=q, levels=levels, tolerance=tolerance, base_dir=base,
            n_processors=ncsize, extend_ratio=refinement_ratio,
            max_extra_levels=max_extra_levels, auto_extend=auto_extend,
            # Never the study's own stdin prompt: a detached job must answer from its
            # own record, and every answer is logged (see jobs.interaction).
            ask=(ctx.ask if ctx is not None else None),
        )
        if ctx is not None:
            ctx.progress(levels_done=len(report.levels), levels_total=len(report.levels),
                         converged=bool(getattr(report, "converged", False)))
        return report

    def _vertical_convergence(self, cfg, *, ctx: Any = None,
                              layer_counts: list[int] | None = None,
                              tolerance: float = 0.05, ncsize: int | None = None,
                              **_ignored):
        from axqua.vertical_convergence import run_vertical_convergence
        kwargs: dict[str, Any] = {"tolerance": tolerance}
        if layer_counts:
            kwargs["counts"] = list(layer_counts)
        if ncsize:
            kwargs["n_processors"] = ncsize
        return run_vertical_convergence(cfg, **kwargs)

    def _calibration(self, cfg, *, ctx: Any = None, flows: list | None = None,
                     launch_mode: str = "run", force: bool = False,
                     prepare_only: bool = False,
                     calibration_quantities: list | None = None,
                     extraction_quantities: list | None = None,
                     vel_err_floor: float | None = None,
                     depth_err_floor: float | None = None, **_ignored):
        from axqua import bayescal

        if flows:
            specs = [bayescal.FlowSpec(**f) for f in flows]
            code = bayescal.run_multiflow_calibration(cfg, specs,
                                                      launch_mode=launch_mode,
                                                      force=force)
        else:
            kwargs: dict[str, Any] = {"prepare_only": prepare_only}
            if calibration_quantities:
                kwargs["calibration_quantities"] = tuple(calibration_quantities)
            if extraction_quantities:
                kwargs["extraction_quantities"] = tuple(extraction_quantities)
            if vel_err_floor is not None:
                kwargs["vel_err_floor"] = vel_err_floor
            if depth_err_floor is not None:
                kwargs["depth_err_floor"] = depth_err_floor
            code = bayescal.run_single_flow_calibration(cfg, **kwargs)
        if code != 0:
            raise errors.SolverError(
                f"the calibration driver exited with code {code}",
                subject=str(cfg.calibration_dir),
                remedy="Read the HydroBayesCal output in the calibration directory.",
                component="hydrobayescal",
                return_code=code,
            )
        return code

    # -- after the run ------------------------------------------------------------
    def postprocess(self, cfg, ctx: Any = None):
        """Flux convergence, wetting and section reports - what ``initial_run.py`` does.

        Only for the kinds where those reports mean something: running a flux analysis
        after a mesh-convergence study would read the last level's listing and report it
        as if it were the case's own.
        """
        from axqua.jobs.model import JobKind
        kind = getattr(getattr(ctx, "spec", None), "kind", None)
        if kind not in (JobKind.STEADY_RUN, JobKind.UNSTEADY_RUN, JobKind.PREPROCESSING):
            return None
        if not any(Path(cfg.model_dir).glob("*.sortie")):
            return None

        from axqua.solvers.telemac.flux_convergence import analyze_flux_convergence
        fc = analyze_flux_convergence(cfg)
        if ctx is not None:
            ctx.progress(flux_imbalance=getattr(fc, "final_imbalance", None))
            for name in ("extracted-fluxes.csv", "flux-convergence.png",
                         "convergence-rate.csv", "convergence-rate.png"):
                ctx.record(name, cfg.model_path(name), kind="figure"
                           if name.endswith(".png") else "table")
        self._wetting_reports(cfg, ctx)
        return fc

    @staticmethod
    def _wetting_reports(cfg, ctx: Any) -> None:
        """A balanced flux budget says nothing about *where* the water is.

        So the wetted-extent report runs too - but it is genuinely optional, and a
        failure here must not fail a job whose solver run succeeded.
        """
        from axqua.workflow import report_wetting
        try:
            report_wetting(cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("wetting report skipped: %s: %s", type(exc).__name__, exc)
            return
        if ctx is not None:
            for name in ("wetting-report.csv", "outlet-profile.csv"):
                ctx.record(name, cfg.model_path(name), kind="table")

    def extract_objective(self, cfg, ctx: Any = None) -> float | None:
        """The number this job was minimising, when there is one.

        A steady run's is the relative flux imbalance - the thing its own convergence
        is judged on - which makes "how good was this run" answerable from the job
        record without opening anything.
        """
        summary = getattr(getattr(ctx, "manifest", None), "summary", {}) or {}
        for key in ("flux_imbalance", "gci", "best_objective"):
            if key in summary:
                try:
                    return float(summary[key])
                except (TypeError, ValueError):
                    return None
        progress = getattr(getattr(ctx, "sink", None), "status", None)
        imbalance = getattr(getattr(progress, "progress", None), "flux_imbalance", None)
        return float(imbalance) if imbalance is not None else None

    def export_qgis_results(self, cfg, ctx: Any = None):
        """Describe what QGIS should open. **No conversion, and no PyQGIS.**

        TELEMAC results are SELAFIN and QGIS reads SELAFIN natively through MDAL, so
        converting them would cost a full copy of a multi-hundred-megabyte file to gain
        nothing (plan §26). The export is therefore a *manifest*: paths plus a hint about
        how to style each one.
        """
        if ctx is None:
            return []
        written: list[Path] = []
        # Results, in the order a user wants them: the thing that was computed first.
        for attr, label, style, variable in (
            ("results_slf", "TELEMAC result (steady)", STYLE_DEPTH, "WATER DEPTH"),
            ("results_unsteady_slf", "TELEMAC result (unsteady)", STYLE_DEPTH,
             "WATER DEPTH"),
            ("results3d_slf", "TELEMAC-3D result", "", ""),
            ("results2d_from_3d_slf", "TELEMAC-3D result (depth-averaged)", STYLE_DEPTH,
             "WATER DEPTH"),
        ):
            name = getattr(cfg, attr, None)
            if not name:
                continue
            path = cfg.model_path(name)
            if not path.exists():
                continue
            ctx.record(label, path, kind="mesh", style=style, variable=variable)
            # The same file carries velocity; a second entry lets the plugin add two
            # differently-styled layers over one dataset without reading it twice.
            ctx.record(f"{label} - velocity", path, kind="mesh", style=STYLE_VELOCITY,
                       variable="SCALAR VELOCITY")
            written.append(path)

        ctx.record("Mesh geometry", cfg.model_path(cfg.geometry_slf), kind="mesh")
        for name, kind in (("mesh-convergence.xlsx", "table"),
                           ("vertical-convergence.xlsx", "table")):
            for found in Path(cfg.postprocessing_dir).rglob(name):
                ctx.record(found.name, found, kind=kind)
        csv = Path(cfg.calibration_dir) / cfg.calibration_csv
        ctx.record("Calibration points", csv, kind="table")
        return written


BACKEND = TelemacBackend()


def _sink_for(ctx: Any, name: str):
    """Tee the solver's own listing into its own rotating file, plus the job sink."""
    if ctx is None:
        return None
    from axqua.jobs.events import TeeSink, TextLogSink
    return TeeSink(ctx.sink, TextLogSink(ctx.solver_sink(name)))
