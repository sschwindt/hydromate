"""End-to-end orchestration of the TELEMAC case-build pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from hydromate import boundary, calibration, dem, hydraulics, mesh, steering
from hydromate.config import Config
from hydromate.env import TelemacRuntime

log = logging.getLogger("hydromate")


@dataclass
class Artifacts:
    geometry_slf: Path | None = None
    boundary_cli: Path | None = None
    friction_tbl: Path | None = None
    cas_file: Path | None = None
    gaia_cas: Path | None = None
    calibration_csv: Path | None = None
    hbc_config: Path | None = None
    rasters: dict[str, Path] = field(default_factory=dict)


def run(cfg: Config, *, validate_env: bool = True, dry_run: bool = False) -> Artifacts:
    """Build the full case. Returns the produced :class:`Artifacts`.

    Parameters
    ----------
    validate_env : check that the TELEMAC environment can be sourced first.
    dry_run : after building, launch the solver once to confirm the case is valid.
    """
    cfg.validate()
    cfg.ensure_dirs()
    art = Artifacts()

    runtime = TelemacRuntime(cfg.telemac)
    if validate_env or dry_run:
        version = runtime.check_available()
        log.info("TELEMAC environment OK (python %s)", version)

    # 1) DEM -> ROI
    log.info("stage 1/5: clipping DEM(s) to the region of interest")
    art.rasters = dem.run(cfg)
    dem_initial = art.rasters["dem_initial_roi"]

    # 2) mesh + bathymetry + geometry SELAFIN
    log.info("stage 2/5: generating mesh, bathymetry and geometry SELAFIN")
    the_mesh, art.geometry_slf = mesh.run(cfg, dem_initial)
    log.info("  mesh: %d nodes, %d elements, %d boundary nodes",
             the_mesh.npoin, the_mesh.nelem, the_mesh.boundary_nodes.size)

    # 3) boundary conditions
    log.info("stage 3/5: writing boundary conditions (.cli)")
    art.boundary_cli, liquids = boundary.write_cli(cfg, the_mesh)
    for lb in liquids:
        log.info("  liquid boundary %d: %s (%d nodes)", lb.index, lb.kind, lb.n_nodes)

    # boundary prescribed values
    inflow = hydraulics.read_inflow(
        Path(cfg.inputs.inflow), steady=(cfg.hydrodynamics.regime == "steady")
    )
    inflow_q = (cfg.hydrodynamics.prescribed_flowrate
                if cfg.hydrodynamics.prescribed_flowrate is not None
                else inflow.steady_value)
    outflow_wse = _resolve_outflow_wse(cfg, inflow_q)
    log.info("  prescribed inflow Q=%.3f m3/s, outflow WSE=%.3f m", inflow_q, outflow_wse)

    # 4) steering files
    log.info("stage 4/5: writing friction table and steering (.cas)")
    art.friction_tbl = steering.write_friction_tbl(cfg)
    art.gaia_cas = steering.write_gaia_cas(cfg)
    art.cas_file = steering.write_cas(
        cfg, liquids, inflow_q, outflow_wse,
        gaia_cas=(art.gaia_cas.name if art.gaia_cas else None),
    )

    # 5) calibration CSV + HydroBayesCal config
    log.info("stage 5/5: building calibration CSV and HydroBayesCal config")
    art.calibration_csv = calibration.build_calibration_csv(cfg)
    art.hbc_config = calibration.emit_hbc_config(cfg, art.calibration_csv)

    if dry_run:
        log.info("dry run: launching %s once to validate the case", cfg.telemac.solver)
        proc = runtime.run_solver(cfg.cas_file, cwd=cfg.model_dir, ncsize=1)
        log.info("solver finished rc=%d", proc.returncode)

    return art


def _resolve_outflow_wse(cfg: Config, inflow_q: float) -> float:
    """Downstream water-surface elevation: explicit, else from a rating curve."""
    if cfg.hydrodynamics.prescribed_elevation is not None:
        return float(cfg.hydrodynamics.prescribed_elevation)
    if cfg.inputs.stage_discharge is not None:
        wse_at = hydraulics.read_stage_discharge(Path(cfg.inputs.stage_discharge))
        return wse_at(inflow_q)
    raise ValueError(
        "No downstream water level available: set hydrodynamics.prescribed_elevation "
        "or provide inputs.stage_discharge (a rating curve)."
    )
