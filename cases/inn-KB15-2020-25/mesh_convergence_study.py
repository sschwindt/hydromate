"""Mesh-convergence (grid-independence) study (Inn case, workflow step 2).

Run after preprocessing.py + initial_run.py (which build and test-run the case).
Runs the SAME steady simulation at the configured discharge on a ladder of four
meshes -- one coarser (x REFINEMENT_RATIO), the configured baseline, and two finer
(/ REFINEMENT_RATIO, / REFINEMENT_RATIO**2) -- samples water depth and scalar
velocity at the ground-truth probe points, and judges convergence on the Grid
Convergence Index (GCI) of the finest grid triplet (with an asymptotic-range
check; the finest successive relative change is the fallback) against
CONV_TOLERANCE. Celik et al. (2008) inform both defaults: successive grids should
differ by a ratio >= 1.3 (closer-spaced grids drown the grid-to-grid differences
in solver noise and destabilize the GCI), and a GCI of 5-10% is acceptable
engineering accuracy (1-2% is high-precision work). Each level is built with
``mesh.size_scale``, so per-zone ``Max Edge Length (m)`` values from the
mesh-zones gpkg scale along with the config sizes.

The study is **resumable**: per-level results carry a ``level.json``, and on a
re-run you are asked whether to reuse the completed levels (re-running only the
missing ones). If the ladder does not reach the GCI goal, the study offers up to
MAX_EXTRA_LEVELS further refinements one at a time -- each preceded by a runtime
estimate (extrapolated from the measured level runtimes) and the standard
mesh-validity check of the candidate size (wall y+, roughness Reynolds number
ks+, cell size vs. ks, turbulence-model consistency; these also appear per level
in the report). In a non-interactive session (nohup/batch) the study stops and
writes the report instead of extending. The .xlsx/.txt reports include the
validity block and a recommended cell size balancing grid independence against
compute time. A ``README.md`` documenting the study folder for external readers
(the central .xlsx summary, the governing equations and quantities examined,
and the purpose of every produced file) is written alongside, ready for data
publication. The study creates and works in its own
``axqua-case/mesh-convergence/`` folder.

The discharge and the outflow stage prescription come from ``case-config.yml``
(same source as preprocessing.py); each mesh is **pre-wetted**: the channel is
seeded with ``INITIAL_DEPTH`` (0.5 m by default) of water on the nodes inside the
``channel`` mesh-zones, and the run is continued from that hotstart so the solver
need not advance the wetting front from the inflow (a large time saving across
the runs). The IC does not affect the steady result the study compares, only its
speed. Set ``INITIAL_DEPTH = None`` to use the production dry start instead.

This runs TELEMAC once per mesh (the finer meshes are large and slow); it is a
deliberate, one-off discretization-error study. Results inform the resolution to
use for the HydroBayesCal calibration (workflow step 3).

Pass ``mode='auto-refinement'`` to run unattended: resume and every extension are
auto-approved (no prompts), so the study keeps refining one grid at a time until
it reaches the tolerance, exhausts AUTO_MAX_EXTRA_LEVELS refinements, or a level
fails to mesh/run -- then it writes the report from the completed levels.

Run: mamba run -n axqua-env python cases/inn-KB15-2020-25/mesh_convergence_study.py
     mamba run -n axqua-env python cases/inn-KB15-2020-25/mesh_convergence_study.py mode='auto-refinement'
"""

from __future__ import annotations

import sys
from pathlib import Path

from axqua import convergence, logging_to, prepare_steady_inputs
from axqua.config import load_config

# case-config.yml lives next to this script (in the case folder)
CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

CONV_TOLERANCE = 0.05   # GCI / relative-change goal (Celik et al. 2008: 5-10%)
INITIAL_DEPTH = 0.5     # pre-wet the channel nodes to this depth [m] (None = dry bed)
REFINEMENT_RATIO = 1.3  # successive grid ratio (>= 1.3 per Celik et al. 2008)
N_COARSER, N_FINER = 1, 2   # ladder: 1 coarser + baseline + 2 finer = 4 meshes
MAX_EXTRA_LEVELS = 3    # further refinements offered when not GCI-converged
AUTO_MAX_EXTRA_LEVELS = 8  # cap on auto-refinement steps (mode='auto-refinement')


def _parse_mode(argv: list[str]) -> str:
    """Read the run mode from argv: ``mode=auto-refinement`` (or a bare
    ``auto``/``auto-refinement`` token) selects auto-refinement; anything else is
    the standard interactive/prompted study."""
    for a in argv:
        token = a.split("=", 1)[1] if a.startswith("mode=") else a
        if token.strip().strip("'\"").lower() in ("auto", "auto-refinement"):
            return "auto-refinement"
    return "standard"


def main(mode: str = "standard") -> None:
    auto = mode == "auto-refinement"
    cfg.ensure_dirs()
    # the study creates and works in its own axqua-case/mesh-convergence/ folder
    # (a sibling of the preprocessing/ simulation/ postprocessing/ phase dirs)
    mc_dir = Path(cfg.postprocessing_dir).parent / "mesh-convergence"
    mc_dir.mkdir(parents=True, exist_ok=True)

    # discharge + outflow rating from the config; pre-wet the channel for speed
    q = prepare_steady_inputs(cfg, prewet_depth=INITIAL_DEPTH)
    levels = convergence.ratio_levels(cfg, ratio=REFINEMENT_RATIO,
                                      n_coarser=N_COARSER, n_finer=N_FINER)
    max_extra = AUTO_MAX_EXTRA_LEVELS if auto else MAX_EXTRA_LEVELS
    prewet = (f"channel pre-wetted to {INITIAL_DEPTH:g} m" if INITIAL_DEPTH is not None
              else "dry-bed start")
    print(f"mesh-convergence study at Q={q:g} m3/s over {len(levels)} meshes "
          f"(x{REFINEMENT_RATIO:g} ladder: {N_COARSER} coarser, baseline, {N_FINER} "
          f"finer; {prewet}) - this runs TELEMAC {len(levels)}+ times and is slow...")
    print(f"working in {mc_dir}")
    if auto:
        print(f"AUTO-REFINEMENT mode: resume and up to {max_extra} further "
              f"refinements are auto-approved; the study refines until it reaches "
              f"the {CONV_TOLERANCE * 100:.0f}% goal, exhausts the refinements, or a "
              "level fails to mesh/run - no prompts.")
    else:
        print("(resume/extension prompts appear on this console; a non-interactive "
              "run reuses completed levels and stops instead of extending)")

    # everything (per-mesh runs, report, log) goes into mesh-convergence/
    with logging_to(mc_dir / cfg.log_file):
        report = convergence.run_mesh_convergence(
            cfg, discharge=q, tolerance=CONV_TOLERANCE, levels=levels,
            base_dir=mc_dir, n_processors=cfg.telemac.n_processors,
            extend_ratio=REFINEMENT_RATIO, max_extra_levels=max_extra,
            auto_extend=auto,
        )

    print(report.format())
    xlsx = report.to_xlsx(mc_dir / "mesh-convergence.xlsx")
    report.save(mc_dir / "mesh-convergence.txt")
    readme = convergence.write_readme(mc_dir, report=report)
    rec = report.recommendation()
    print(f"\nRECOMMENDED cell size: {rec['cell_size']:.3f} m - {rec['reason']}")
    print(f"styled report -> {xlsx}")
    print(f"dataset README (for sharing/review) -> {readme}")


if __name__ == "__main__":
    main(_parse_mode(sys.argv[1:]))
