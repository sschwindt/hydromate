"""Mesh-convergence (grid-independence) study (TEMPLATE, workflow step 2).

Run after preprocessing.py + initial_run.py (which build and test-run the case).
Runs the SAME steady simulation at a constant discharge on five meshes -- the
configured baseline plus two coarser (+40% / +20% cell size) and two finer
(-20% / -40%) -- samples water depth and scalar velocity at the ground-truth
probe points, and quantifies the relative change between successive refinements
(plus an observed order of convergence and a Grid Convergence Index). Everything
lands in the produced ``tm-simulation/postprocessing/mesh-convergence/`` folder
(created during preprocessing.py): a styled .xlsx report with a recommended cell
size balancing grid independence against compute time, the per-mesh runs, and the
study log.

This runs TELEMAC five times (the finer meshes are large and slow); it is a
deliberate, one-off discretization-error study. Results inform the resolution to
use for the HydroBayesCal calibration (workflow step 3).

Run: mamba run -n hydromate-env python cases/<your-case>/mesh_convergence_study.py
"""

from __future__ import annotations

from pathlib import Path

from hydromate import convergence, logging_to, synthesize_outflow_rating
from hydromate.config import load_config

# case-config.yml lives next to this script (in the case folder)
CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

DISCHARGE = 47.0        # constant steady discharge [m3/s] for every mesh
CONV_TOLERANCE = 0.02   # convergence tolerance on the QoI (2%)
BANK_SLOPE = 1.0        # trapezoidal channel banks (H:V) for the synthesised rating
# five meshes as fractional cell-size offsets from the config size (coarse->fine)
PERCENTS = (0.40, 0.20, 0.0, -0.20, -0.40)


def prepare_inputs(cfg, q: float) -> None:
    """Prescribe the discharge and synthesise the inflow + outflow rating if missing
    (so every mesh in the study builds from the same boundary conditions)."""
    cfg.hydrodynamics.prescribed_flowrate = q
    if cfg.inputs.inflow is None or not Path(cfg.inputs.inflow).exists():
        p = cfg.preprocessing_path("inflow-constant.csv")
        p.write_text(f"datetime,Q\n2020-11-01 00:00,{q}\n2020-11-01 01:00,{q}\n")
        cfg.inputs.inflow = p
    sd = cfg.inputs.stage_discharge
    if cfg.hydrodynamics.outflow_condition == "stage_discharge" \
            and (sd is None or not Path(sd).exists()):
        cfg.inputs.stage_discharge = synthesize_outflow_rating(cfg, q, side_slope=BANK_SLOPE)


def main() -> None:
    cfg.ensure_dirs()
    # the mesh-convergence folder is normally created by preprocessing.py;
    # double-check it exists (e.g. if the study is run on its own).
    mc_dir = cfg.postprocessing_path("mesh-convergence")
    mc_dir.mkdir(parents=True, exist_ok=True)

    prepare_inputs(cfg, DISCHARGE)
    levels = convergence.percent_levels(cfg, PERCENTS)
    print(f"mesh-convergence study at Q={DISCHARGE:g} m3/s over {len(levels)} meshes "
          f"(+40%/+20% coarser, baseline, -20%/-40% finer) - this runs TELEMAC "
          f"{len(levels)} times and is slow...")

    # everything (per-mesh runs, report, log) goes into mesh-convergence/
    with logging_to(mc_dir / cfg.log_file):
        report = convergence.run_mesh_convergence(
            cfg, discharge=DISCHARGE, tolerance=CONV_TOLERANCE, levels=levels,
            base_dir=mc_dir, n_processors=cfg.telemac.n_processors,
        )

    print(report.format())
    xlsx = report.to_xlsx(mc_dir / "mesh-convergence.xlsx")
    report.save(mc_dir / "mesh-convergence.txt")
    rec = report.recommendation()
    print(f"\nRECOMMENDED cell size: {rec['cell_size']:.3f} m - {rec['reason']}")
    print(f"styled report -> {xlsx}")


if __name__ == "__main__":
    main()
