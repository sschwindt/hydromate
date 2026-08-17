"""Vertical-layer convergence study for the TELEMAC-3D case (optional, 3D analogue
of mesh_convergence_study.py).

Where mesh_convergence_study.py varies the *horizontal* cell size, this varies the
**number of sigma layers** (NUMBER OF HORIZONTAL LEVELS) on the same horizontal mesh
to demonstrate that the depth-resolved 3D solution is independent of the vertical
discretisation. For a ladder of layer counts (few -> many, derived from the count
add3d.py infers, or set explicitly via LAYER_COUNTS) it runs the hotstarted,
non-hydrostatic 3D case once per count, samples depth-averaged water depth and
scalar velocity at the ground-truth probe points, and reports the relative change
between successive counts plus an observed order of convergence and a GCI.

Ordering: this is the LAST step of the 3D path and presupposes the whole 2D path.
Run it only after (a) the 2D hotstart exists (preprocessing.py + initial_run.py ->
r2d.slf), (b) the 2D mesh-convergence study has fixed the horizontal resolution
(mesh_convergence_study.py) - this study reuses that horizontal mesh unchanged and
only varies the vertical layers, and (c) add3d.py has produced a working 3D case.
Just as the horizontal cell size needed step 2, the vertical discretization dz needs
this separate grid-independence study. It hotstarts each 3D run from r2d.slf and does
NOT rebuild the mesh; it runs telemac3d once per layer count and is slow (a
deliberate, one-off study). Outputs land in tm-simulation/postprocessing/vertical-convergence/.

Run: mamba run -n axqua-env python cases/<your-case>/vertical_convergence_3d.py
"""

from __future__ import annotations

from pathlib import Path

from axqua import logging_to, run_vertical_convergence
from axqua.config import load_config

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

CONV_TOLERANCE = 0.02     # convergence tolerance on the QoI (2%)
# explicit vertical-layer counts to test (few -> many); None = auto ladder from the
# count add3d.py infers (roughly half / baseline / double / triple the intervals)
LAYER_COUNTS = None       # e.g. (3, 5, 9, 13)
TOTAL_TIME_FACTOR = 4.0   # simulated time per run = this many reach flow-through times


def main() -> None:
    cfg.ensure_dirs()
    results_2d = cfg.model_path(cfg.results_slf)
    if not results_2d.exists():
        print(f"no 2D result at {results_2d}.")
        print("run preprocessing.py then initial_run.py first (they produce r2d.slf).")
        return

    vc_dir = cfg.postprocessing_path("vertical-convergence")
    vc_dir.mkdir(parents=True, exist_ok=True)
    print("vertical-layer convergence study (varies NUMBER OF HORIZONTAL LEVELS on the "
          "same horizontal mesh) - this runs telemac3d once per layer count and is slow...")

    with logging_to(vc_dir / cfg.log_file):
        report = run_vertical_convergence(
            cfg, counts=LAYER_COUNTS, tolerance=CONV_TOLERANCE, base_dir=vc_dir,
            n_processors=cfg.telemac.n_processors, total_time_factor=TOTAL_TIME_FACTOR,
        )

    print(report.format())
    xlsx = report.to_xlsx(vc_dir / "vertical-convergence.xlsx")
    report.save(vc_dir / "vertical-convergence.txt")
    rec = report.recommendation()
    print(f"\nRECOMMENDED vertical levels: {rec['n_levels']} (dz~{rec['dz']:.3f} m) - "
          f"{rec['reason']}")
    print(f"styled report -> {xlsx}")


if __name__ == "__main__":
    main()
