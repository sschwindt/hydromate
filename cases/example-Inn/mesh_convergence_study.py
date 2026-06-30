"""Mesh-convergence (grid-independence) study (Inn case, workflow step 2).

Run after preprocessing.py + initial_run.py (which build and test-run the case).
Runs the SAME steady simulation at the configured discharge on five meshes -- the
configured baseline plus two coarser (+40% / +20% cell size) and two finer
(-20% / -40%) -- samples water depth and scalar velocity at the ground-truth
probe points, and quantifies the relative change between successive refinements
(plus an observed order of convergence and a Grid Convergence Index). The study
creates and works in its own ``hydromate-case/mesh-convergence/`` folder: a styled
.xlsx report with a recommended cell size balancing grid independence against
compute time, the per-mesh runs, and the study log.

The discharge and the outflow stage prescription come from ``case-config.yml``
(same source as preprocessing.py); each mesh is **pre-wetted**: the channel is
seeded with ``INITIAL_DEPTH`` (0.5 m by default) of water on the nodes inside the
``channel`` mesh-zones, and the run is continued from that warm start so the solver
need not advance the wetting front from the inflow (a large time saving across the
five runs). The IC does not affect the steady result the study compares, only its
speed. Set ``INITIAL_DEPTH = None`` to use the production dry start instead.

This runs TELEMAC five times (the finer meshes are large and slow); it is a
deliberate, one-off discretization-error study. Results inform the resolution to
use for the HydroBayesCal calibration (workflow step 3).

Run: mamba run -n hydromate-env python cases/example-Inn/mesh_convergence_study.py
"""

from __future__ import annotations

from pathlib import Path

from hydromate import convergence, logging_to, prepare_steady_inputs
from hydromate.config import load_config

# case-config.yml lives next to this script (in the case folder)
CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

CONV_TOLERANCE = 0.02   # convergence tolerance on the QoI (2%)
INITIAL_DEPTH = 0.5     # pre-wet the channel nodes to this depth [m] (None = dry bed)
# five meshes as fractional cell-size offsets from the config size (coarse->fine)
PERCENTS = (0.40, 0.20, 0.0, -0.20, -0.40)


def main() -> None:
    cfg.ensure_dirs()
    # the study creates and works in its own hydromate-case/mesh-convergence/ folder
    # (a sibling of the preprocessing/ simulation/ postprocessing/ phase dirs)
    mc_dir = Path(cfg.postprocessing_dir).parent / "mesh-convergence"
    mc_dir.mkdir(parents=True, exist_ok=True)

    # discharge + outflow rating from the config; pre-wet the channel for speed
    q = prepare_steady_inputs(cfg, prewet_depth=INITIAL_DEPTH)
    levels = convergence.percent_levels(cfg, PERCENTS)
    prewet = (f"channel pre-wetted to {INITIAL_DEPTH:g} m" if INITIAL_DEPTH is not None
              else "dry-bed start")
    print(f"mesh-convergence study at Q={q:g} m3/s over {len(levels)} meshes "
          f"(+40%/+20% coarser, baseline, -20%/-40% finer; {prewet}) - this runs "
          f"TELEMAC {len(levels)} times and is slow...")
    print(f"working in {mc_dir}")

    # everything (per-mesh runs, report, log) goes into mesh-convergence/
    with logging_to(mc_dir / cfg.log_file):
        report = convergence.run_mesh_convergence(
            cfg, discharge=q, tolerance=CONV_TOLERANCE, levels=levels,
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
