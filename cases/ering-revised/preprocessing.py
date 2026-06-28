"""Preprocessing + case build for the Inn case (workflow step 1).

Assembles a complete, ready-to-run TELEMAC-2D case at a constant discharge:
clips the DEM(s), builds the mesh (anisotropic + roughness), classifies the
liquid boundaries and writes the case into ``tm-simulation/simulation/`` -- the
final mesh ``geometry.slf``, the boundary-conditions ``boundaries.cli``, the
friction ``friction.tbl`` and the steering ``steady2d.cas`` -- plus the
HydroBayesCal artifacts in ``calibration-validation/`` and the ground-truth /
DEM-clip products in ``preprocessing/``. A constant inflow and the outflow
stage-discharge rating curve are synthesised if missing.

It does NOT launch the solver: run ``initial_run.py`` next to test-run the built
case (that confirms it does not crash, ending the preprocessing step). Then run
``mesh_convergence_study.py``, and finally HydroBayesCal.

Run: mamba run -n hydromate-env python cases/example-Inn/preprocessing.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

from hydromate import pipeline, setup_logging, synthesize_outflow_rating
from hydromate.config import load_config

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

DISCHARGE = 2.0        # constant steady discharge [m3/s] the case is built for
BANK_SLOPE = 1.0        # trapezoidal channel banks (H:V) for the synthesised rating


def prepare_constant_discharge(cfg, q: float) -> None:
    """Prescribe a constant discharge and synthesise a tiny inflow series if none."""
    cfg.hydrodynamics.prescribed_flowrate = q
    if cfg.inputs.inflow is None or not Path(cfg.inputs.inflow).exists():
        path = cfg.preprocessing_path("inflow-constant.csv")
        path.write_text(f"datetime,Q\n2020-11-01 00:00,{q}\n2020-11-01 01:00,{q}\n")
        cfg.inputs.inflow = path
        print(f"using synthesised constant inflow Q={q:g} m3/s -> {path.name}")


def prepare_outflow_rating(cfg, q: float) -> None:
    """Synthesise the outflow stage-discharge curve from the geodata if missing
    (width from the outflow boundary line, bed + reach slope from the DEM,
    trapezoidal banks BANK_SLOPE, roughness from friction.boundary_*)."""
    if cfg.hydrodynamics.outflow_condition != "stage_discharge":
        return
    sd = cfg.inputs.stage_discharge
    if sd is None or not Path(sd).exists():
        path = synthesize_outflow_rating(cfg, q, side_slope=BANK_SLOPE)
        cfg.inputs.stage_discharge = path
        print(f"generated outflow rating curve at Q={q:g} m3/s -> {path}")


def main() -> None:
    cfg.ensure_dirs()
    # the mesh-convergence study (step 2) writes into this folder; create it now
    # so it already exists in the produced tm-simulation/ tree after preprocessing
    cfg.postprocessing_path("mesh-convergence").mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.model_path(cfg.log_file))   # build log in simulation/
    print(f"case '{cfg.name}' -> building into {cfg.model_dir}")

    prepare_constant_discharge(cfg, DISCHARGE)
    prepare_outflow_rating(cfg, DISCHARGE)

    try:
        art = pipeline.run(cfg, validate_env=False, dry_run=False)
    except Exception as exc:  # noqa: BLE001 - report what is still missing
        print(f"build not ready: {type(exc).__name__}: {exc}")
        print("complete the inputs and telemac.pysource in case-config.yml, then re-run.")
        return

    # keep a copy of the rating curve next to the case for traceability
    if cfg.inputs.stage_discharge and Path(cfg.inputs.stage_discharge).exists():
        shutil.copy(cfg.inputs.stage_discharge, cfg.model_path("rating-curve.csv"))

    print(f"\nbuilt the TELEMAC case in {cfg.model_dir}:")
    print(f"  mesh      : {art.geometry_slf.name}")
    print(f"  boundary  : {art.boundary_cli.name}")
    print(f"  friction  : {art.friction_tbl.name}")
    print(f"  steering  : {art.cas_file.name}")
    print(f"  HBC config: {art.hbc_config}")
    print(f"  convergence dir ready: {cfg.postprocessing_path('mesh-convergence')}")
    print("next: test-run it with  python cases/example-Inn/initial_run.py")


if __name__ == "__main__":
    main()
