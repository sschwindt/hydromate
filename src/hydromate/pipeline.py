"""End-to-end orchestration of the TELEMAC case-build pipeline."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from hydromate import boundary, calibration, dem, hydraulics, mesh, steering
from hydromate.config import Config
from hydromate.env import TelemacRuntime
from hydromate.logsetup import log_step, setup_logging

log = logging.getLogger("hydromate")


@dataclass
class Artifacts:
    geometry_slf: Path | None = None
    boundary_cli: Path | None = None
    friction_tbl: Path | None = None
    initial_conditions: Path | None = None
    cas_file: Path | None = None
    unsteady_cas: Path | None = None
    liquid_boundaries: Path | None = None
    gaia_cas: Path | None = None
    ground_truth: Path | None = None
    calibration_csv: Path | None = None
    hbc_config: Path | None = None
    rasters: dict[str, Path] = field(default_factory=dict)


def run(cfg: Config, *, validate_env: bool = True, dry_run: bool = False,
        log_to_file: bool = True) -> Artifacts:
    """Build the full case. Returns the produced :class:`Artifacts`.

    Parameters
    ----------
    validate_env : check that the TELEMAC environment can be sourced first.
    dry_run : after building, launch the solver once to confirm the case is valid.
    log_to_file : attach this build's ``<model_dir>/hydromate.log`` (set False
        when a caller already routes everything to its own compound log, e.g. the
        mesh-convergence study logging the per-level builds to postprocessing/).
    """
    cfg.validate()
    cfg.ensure_dirs()
    # compound logfile for the build, in the simulation (model) output folder
    if log_to_file:
        setup_logging(cfg.model_path(cfg.log_file))
    art = Artifacts()
    t_start = time.perf_counter()
    log.info("build start: case '%s' -> %s", cfg.name, cfg.model_dir)

    runtime = TelemacRuntime(cfg.telemac)
    if validate_env or dry_run:
        with log_step("validate TELEMAC environment"):
            version = runtime.check_available()
            log.info("TELEMAC environment OK (python %s)", version)

    # 1) DEM -> ROI
    with log_step("stage 1/5: clip DEM(s) to the region of interest"):
        art.rasters = dem.run(cfg)
        dem_initial = art.rasters["dem_initial_roi"]

    # 2) mesh + bathymetry + geometry SELAFIN
    with log_step("stage 2/5: generate mesh, bathymetry and geometry SELAFIN"):
        the_mesh, art.geometry_slf = mesh.run(cfg, dem_initial)
        log.info("  mesh: %d nodes, %d elements, %d boundary nodes",
                 the_mesh.npoin, the_mesh.nelem, the_mesh.boundary_nodes.size)

    # 3) boundary conditions
    with log_step("stage 3/5: write boundary conditions (.cli)"):
        art.boundary_cli, liquids = boundary.write_cli(cfg, the_mesh)
        for lb in liquids:
            log.info("  liquid boundary %d: %s (%d nodes)", lb.index, lb.kind, lb.n_nodes)

        # boundary prescribed values. Read the full series (steady=False) so a
        # hydrograph is detected regardless of regime; the steady initial run still
        # uses the representative steady discharge.
        inflow = hydraulics.read_inflow(Path(cfg.inputs.inflow), steady=False)
        inflow_q = (cfg.hydrodynamics.prescribed_flowrate
                    if cfg.hydrodynamics.prescribed_flowrate is not None
                    else inflow.steady_value)
        if cfg.hydrodynamics.outflow_condition == "free":
            outflow_wse = None
            log.info("  prescribed inflow Q=%.3f m3/s, free (Neumann) outflow", inflow_q)
        else:
            outflow_wse = _resolve_outflow_wse(cfg, inflow_q)
            log.info("  prescribed inflow Q=%.3f m3/s, outflow WSE=%.3f m (%s)",
                     inflow_q, outflow_wse, cfg.hydrodynamics.outflow_condition)

    # 4) steering files
    with log_step("stage 4/5: write friction table and steering (.cas)"):
        art.friction_tbl = steering.write_friction_tbl(cfg)
        art.gaia_cas = steering.write_gaia_cas(cfg)
        gaia_name = art.gaia_cas.name if art.gaia_cas else None
        # turbulence model selected from the actual mesh resolution + velocity guess
        turb_model, turb_why = steering.select_turbulence_model(cfg, the_mesh)
        log.info("  turbulence model %d (%s): %s", turb_model,
                 steering.TURB_NAMES.get(turb_model, "?"), turb_why)
        # initial condition. Default is a DRY START - only a thin plug at the inflow
        # is wetted (a fully dry bed makes DEBIMP abort at the prescribed-Q inflow);
        # the channel-wide warm start is used only when prewet_depth is set (e.g. the
        # mesh-convergence study). Both continue from the written SELAFIN.
        prev_comp = None
        if cfg.hydrodynamics.prewet_depth is not None:
            art.initial_conditions = steering.write_initial_conditions(cfg, the_mesh)
            prev_comp = art.initial_conditions.name
        else:
            art.initial_conditions = steering.write_dry_start_conditions(cfg, the_mesh)
            prev_comp = art.initial_conditions.name if art.initial_conditions else None
        # the initial run is ALWAYS steady (steady2d.cas)
        art.cas_file = steering.write_cas(
            cfg, liquids, inflow_q, outflow_wse, gaia_cas=gaia_name,
            previous_computation=prev_comp, turbulence_model=turb_model,
        )
        # additionally write the unsteady hydrograph case when a varying inflow
        # series is available (a constant-Q inflow needs no unsteady run)
        if _has_hydrograph(inflow):
            art.liquid_boundaries = steering.write_liquid_boundaries(
                cfg, liquids, inflow, _outflow_wse_fn(cfg))
            art.unsteady_cas = steering.write_cas(
                cfg, liquids, float(inflow.discharge[0]), outflow_wse, gaia_cas=gaia_name,
                previous_computation=prev_comp, turbulence_model=turb_model,
                unsteady=True, liquid_boundaries_file=art.liquid_boundaries.name,
                duration=_hydrograph_duration(inflow), out_name=cfg.unsteady_cas_file,
            )
            log.info("  unsteady hydrograph case -> %s (Q(t) from %s)",
                     art.unsteady_cas.name, art.liquid_boundaries.name)
        else:
            log.info("  inflow is a constant discharge; no unsteady2d.cas written")

    # 5) ground-truth -> calibration CSV + HydroBayesCal config
    with log_step("stage 5/5: compile ground truth, calibration CSV and HydroBayesCal config"):
        art.ground_truth = calibration.compile_ground_truth(cfg)
        if art.ground_truth:
            log.info("  compiled tidy ground-truth table -> %s", art.ground_truth)
        art.calibration_csv = calibration.build_calibration_csv(cfg)
        art.hbc_config = calibration.emit_hbc_config(cfg, art.calibration_csv)

    if dry_run:
        with log_step(f"dry run: launch {cfg.telemac.solver} once to validate the case"):
            proc = runtime.run_solver(cfg.cas_file, cwd=cfg.model_dir, ncsize=1)
            log.info("solver finished rc=%d", proc.returncode)

    log.info("build done: case '%s' in %.2fs", cfg.name, time.perf_counter() - t_start)
    return art


def _has_hydrograph(inflow) -> bool:
    """True when the inflow carries a genuinely varying time series (a hydrograph),
    not a single value or a constant-Q placeholder."""
    import numpy as np

    q = np.asarray(inflow.discharge, dtype=float)
    return inflow.times_s is not None and q.size > 1 and float(np.ptp(q)) > 1e-6


def _hydrograph_duration(inflow) -> float:
    """Total simulated time [s] spanned by the inflow hydrograph."""
    import numpy as np

    t = np.asarray(inflow.times_s, dtype=float)
    return float(t[-1] - t[0]) or 3600.0


def _outflow_wse_fn(cfg: Config):
    """Callable Q -> outflow WSE for the hydrograph file, matching the outflow
    condition (free outflow prescribes nothing)."""
    cond = cfg.hydrodynamics.outflow_condition
    if cond == "free":
        return None
    if cond == "elevation":
        wse = float(cfg.hydrodynamics.prescribed_elevation)
        return lambda q: wse
    return hydraulics.read_stage_discharge(Path(cfg.inputs.stage_discharge))


def _resolve_outflow_wse(cfg: Config, inflow_q: float) -> float:
    """Downstream water-surface elevation for a prescribed-elevation outflow.

    ``stage_discharge`` (default) reads the rating curve and interpolates the WSE
    at the simulated discharge; ``elevation`` uses the fixed prescribed value.
    """
    cond = cfg.hydrodynamics.outflow_condition
    if cond == "elevation":
        if cfg.hydrodynamics.prescribed_elevation is None:
            raise ValueError(
                "outflow_condition: elevation needs hydrodynamics.prescribed_elevation"
            )
        return float(cfg.hydrodynamics.prescribed_elevation)
    # stage_discharge: look up the rating curve at the simulated Q
    if cfg.inputs.stage_discharge is None:
        raise ValueError(
            "outflow_condition: stage_discharge needs inputs.stage_discharge (a Q-h "
            "rating CSV). Generate one with `hydromate rating` from a Manning/"
            "Strickler value and the channel geometry (normal-flow conditions)."
        )
    wse_at = hydraulics.read_stage_discharge(Path(cfg.inputs.stage_discharge))
    return wse_at(inflow_q)
