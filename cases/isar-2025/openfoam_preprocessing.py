"""Build the OpenFOAM free-surface case (OPTIONAL 3D extension, after step 1b).

Turns the converged TELEMAC 2D result into a complete, ready-to-run ``interFoam``
case under ``axqua-case/openfoam/``. It does NOT launch anything: run
``openfoam_run.py`` next.

Why this exists
---------------
A 2D depth-averaged model cannot give the vertical structure of the flow, and a
steady single-phase 3D solver (``simpleFoam``) cannot represent a water surface that
has a gradient - which every real reach does. That leaves a two-phase VOF solver,
and with it the air phase, which is where OpenFOAM river models usually come apart:
the air drives the Courant number, collapses the time step and destabilises the
outlet, all for a phase nobody is modelling for its own sake.

Three things here address that directly:

* **The lid follows the water.** The mesh is only built ``openfoam.freeboard`` metres
  above the 2D free surface, so most of the air simply does not exist as cells.
* **The velocity is capped.** ``system/fvConstraints`` carries a ``limitVelocity``
  constraint at several times the reach's own water speed. Water never reaches it;
  a runaway air jet does.
* **The run is staged.** Stage 1 settles the interface from the depth-averaged
  hotstart at a tight Courant number; stage 2 runs at the full one with semi-implicit
  MULES. See ``openfoam_run.py``.

And the mesh is written directly, not snapped: a river bed is a height field, so it
is *followed* by a structured, all-hexahedral, terrain-following grid rather than
approximated by snappyHexMesh's castellate-and-snap. See
``axqua.solvers.openfoam.polymesh`` for the reasoning.

Prerequisites: ``preprocessing.py`` and ``initial_run.py`` have run, so the case has
a converged ``r2d.slf``; and ``openfoam.bashrc`` in case-config.yml points at your
OpenFOAM ``etc/bashrc``. Ideally ``mesh_convergence_study.py`` has run too - this
case reuses the horizontal resolution decision implicitly through ``cell_size``.

Run: mamba run -n axqua-env python cases/<your-case>/openfoam_preprocessing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from axqua import setup_logging
from axqua.config import load_config
from axqua.prerun import ensure_seed
from axqua.solvers.openfoam import build_case, estimate_cells, load_hotstart, summarise

# optional CLI arg selects the scenario config, e.g.
#   python openfoam_preprocessing.py case-config-greenampt.yml
CONFIG = Path(__file__).resolve().parent / (
    sys.argv[1] if len(sys.argv) > 1 else "case-config.yml")
cfg = load_config(CONFIG)

# Refuse to build past this many cells without being asked again. A terrain-following
# mesh is cheap to generate and expensive to run, so the easiest mistake here is to
# halve cell_size and discover the cost only once the solver is running.
CELL_BUDGET = 4_000_000

# Uncheck the TELEMAC pre-run for this run only, without editing the config.
#   None  - follow openfoam.pre_run.enabled in case-config.yml (normally True)
#   False - never start TELEMAC: seed from an existing r2d.slf if there is one,
#           otherwise build COLD under a flat lid (many times more air cells, plus
#           the whole filling transient to pay for in the slowest solver you have)
#   True  - force it on even if the config disables it
PRE_RUN: bool | None = None


def main() -> None:
    case_dir = cfg.openfoam_case_dir
    case_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(case_dir / cfg.log_file)
    print(f"case '{cfg.name}' -> building the OpenFOAM case into {case_dir}")

    # TELEMAC first: it answers in minutes what OpenFOAM would otherwise spend hours
    # discovering - where the water is, how deep it is and roughly where its surface
    # sits. Reuses the case's own converged r2d.slf when there is one, else runs a
    # coarse pre-run of its own. The 3D surface is still solved and free to leave it.
    seed = ensure_seed(cfg, enabled=PRE_RUN)
    for line in seed.summary():
        print(line)

    state = load_hotstart(cfg, seed.path) if seed.ok else None
    if state is not None:
        print()
        for line in state.summary():
            print(line)

    n = estimate_cells(cfg, state=state)
    print(f"\n{cfg.openfoam.cell_size:g} m lattice x {cfg.openfoam.n_layers} layers "
          f"-> about {n:,} cells")
    if n > CELL_BUDGET:
        print(f"that is over the {CELL_BUDGET:,}-cell budget set in this script. "
              "Coarsen openfoam.cell_size / n_layers, or raise CELL_BUDGET if you "
              "mean it.")
        return

    try:
        art = build_case(cfg, state=state)
    except Exception as exc:  # noqa: BLE001 - report what is still missing
        print(f"build not ready: {type(exc).__name__}: {exc}")
        return

    print()
    for line in summarise(art):
        print(line)

    report = art.report
    if report is not None and not report.usable:
        print("\nthe mesh is NOT runnable (zero or negative cell volumes above). "
              "Coarsen openfoam.cell_size or raise min_column_height and rebuild.")
    elif report is not None and report.marginal_faces:
        print(f"\ncheckMesh will report {report.marginal_faces} marginal face(s) out "
              f"of {report.n_faces:,}. On a mesh cut from a real DEM that is normally "
              "a survey step or seam rather than a meshing fault, and the run is "
              "unaffected - openfoam_run.py proceeds.")
    print(f"\nnext: python {Path(__file__).with_name('openfoam_run.py').name}")


if __name__ == "__main__":
    main()
