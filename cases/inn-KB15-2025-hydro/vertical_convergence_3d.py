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
# Explicit vertical-layer counts to test (few -> many).
#
# 2026-08-10: the ladder was (4, 7, 10, 14) and did NOT converge - because at 7+
# levels the study leaves the regime where a rough-wall law is even defined. With
# LAW OF BOTTOM FRICTION 5, telemac3d's tfond.f evaluates u*^2 =
# (kappa/ln(30 dz/ks))^2 U^2 at the FIRST layer, so the effective bed friction is a
# function of the layer count at fixed ks: refining multiplied Cf by 4.6x from 4 to
# 14 levels, which is what the diverging QoI was measuring. A wall function needs
# its first node above the roughness tops, dz > ks; with ks = 0.2 m that caps the
# level count at 5 for the median wetted depth (0.83 m) and 3 at p25 (0.50 m).
# (3, 4, 5) is the admissible ladder, and its dz ratios 1.5 / 1.33 both clear the
# r >= 1.3 that Celik et al. (2008) require. L4 is already on disk from the previous
# ladder with byte-identical steering, so only 3 and 5 are re-run here; build the
# 3-level report over 3,4,5 with RESUME-runbook/build_vertical_report_offline.py.
# See postprocessing/vertical-convergence/FINDING-wall-function-limit.md.
LAYER_COUNTS = (3, 5)
TOTAL_TIME_FACTOR = 4.0   # (ignored when TARGET_TIME is set)
# KB15 3D runs use the continuation hotstart (fast spin-up from the equilibrium 2D
# field; the Spalart-Allmaras source-term fix in simulation/user_fortran/ is compiled
# in automatically) with a firm depth floor - the full cold-start / reach-flow-through
# study would be infeasible at ~0.3 steps/s.
HOTSTART = "continuation"
# Hold the SIMULATED PHYSICAL TIME constant across layer counts (not the step count):
# a finer grid has a smaller CFL dt, so it needs proportionally more steps to reach the
# same time. A fixed step cap instead froze finer levels at a much earlier transient
# (L4 saw 104 s but L14 only 25 s), which masqueraded as vertical non-convergence.
# Each level runs round(TARGET_TIME / dt_level) steps.
TARGET_TIME = 2500.0      # s of simulated time every level must reach. Validated on L4:
                          # with the prescribed inflow (47.3 m3/s) forced, the outflow
                          # relaxes from the over-full continuation state (~65) down to
                          # 47.3, reaching <0.5% mass imbalance by ~1400 s and fully
                          # settled (<0.2%) by ~2100 s. This drain-down is a free-surface
                          # relaxation set by the horizontal mesh, so T* is ~independent
                          # of the layer count -> 2500 s converges every level (with margin).
N_TIME_STEPS = None       # (superseded by TARGET_TIME)
MIN_DEPTH = 0.10          # m; firmer floor for continuation stability on dry floodplain


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
            hotstart=HOTSTART, n_time_steps=N_TIME_STEPS, target_time=TARGET_TIME,
            min_depth=MIN_DEPTH,
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
