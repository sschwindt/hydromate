"""Build a TELEMAC-3D case from the converged 2D run (optional 3D extension).

The 3D path runs **after the whole 2D path**: a 3D simulation is only built once the
2D run has produced its hotstart result (run ``initial_run.py`` first -> ``r2d.slf``)
AND the 2D mesh-convergence study (``mesh_convergence_study.py``) has settled the
horizontal resolution - the 3D case reuses that same horizontal mesh. Choosing the
number of vertical layers is then a *separate* convergence question (the 2D study
never touched ``dz``); run ``vertical_convergence_3d.py`` after this.

After ``initial_run.py`` has produced the converged 2D steady result
(``tm-simulation/simulation/r2d.slf``), this writes a ``<case-name>3d.cas`` for
``telemac3d.py`` alongside the 2D case, **without rebuilding** the mesh:

* it **hotstarts** the 3D run from the 2D result with the TELEMAC v9+ true 2D->3D
  continuation (``FILE FOR 2D CONTINUATION``; a commented ``CONSTANT DEPTH`` cold-start
  fallback is written too, for when the continuation inflow is too thin - see below);
* it **infers the number of sigma layers** from the 2D depth/cell size so the
  layer thickness ``dz`` lands near the horizontal cell size (``dz ~ dx/2``, never
  more than 4x finer) - TELEMAC has no Delft3D-style constant-dz z-layers, only
  sigma planes, so the level count is what we tune;
* it picks the **turbulence model** with the same quality check as the 2D case
  (k-epsilon / Smagorinski / Spalart-Allmaras), runs **non-hydrostatic**, and sizes
  the fixed time step for a Courant number of 0.6 (3D has no DESIRED COURANT NUMBER).

Pass ``--run`` to also launch ``telemac3d.py`` on the produced case (needs a real
``telemac.pysource`` in case-config.yml). Otherwise it only writes the steering and
prints the command to run it.

Run: mamba run -n axqua-env python cases/<your-case>/add3d.py [--run]
"""

from __future__ import annotations

import sys
from pathlib import Path

from axqua import build_3d_cas, setup_logging
from axqua.config import load_config
from axqua.env import TelemacRuntime

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)


def main(run: bool = False) -> None:
    results_2d = cfg.model_path(cfg.results_slf)
    if not results_2d.exists():
        print(f"no 2D result at {results_2d}.")
        print("run  python cases/<your-case>/initial_run.py  first (it produces r2d.slf).")
        return

    setup_logging(cfg.model_path(cfg.log_file))   # append to the simulation log
    print(f"building the TELEMAC-3D case from {results_2d.name}")

    setup = build_3d_cas(cfg)
    print(f"\nwrote {setup.cas.name}:")
    print(f"  vertical    : {setup.n_levels} sigma levels (dz~{setup.dz:.2f} m, "
          f"dx~{setup.dx:.2f} m, depth~{setup.depth:.2f} m)")
    print(f"  turbulence  : H={setup.h_turbulence} V={setup.v_turbulence} "
          f"({setup.turbulence_reason})")
    print(f"  time step   : {setup.time_step} s for Courant {setup.courant:g} "
          f"({setup.n_time_steps} steps)")
    print("  solver      : non-hydrostatic, v9 2D-continuation hotstart from r2d.slf")
    print("  note        : if telemac3d aborts with 'DEBIMP_3D: PROBLEM ON BOUNDARY' "
          "(thin/supercritical")
    print("                inflow from the continuation), swap in the commented "
          "CONSTANT DEPTH fallback")
    print(f"                in {setup.cas.name} (a deep uniform cold-start seed).")

    if not run:
        print(f"\nnext: run it with  python cases/<your-case>/add3d.py --run")
        print(f"or directly:  telemac3d.py {setup.cas.name}  (in {cfg.model_dir})")
        return

    print(f"\nlaunching telemac3d.py on {setup.cas.name} ...")
    runtime = TelemacRuntime(cfg.telemac)
    try:
        runtime.check_available()
        proc = runtime.run_solver(setup.cas.name, cwd=cfg.model_dir,
                                  ncsize=cfg.telemac.n_processors, solver="telemac3d")
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(f"could not run telemac3d: {type(exc).__name__}: {exc}")
        return
    if proc.returncode != 0:
        print(f"FAILED - telemac3d returned {proc.returncode}; "
              f"see {cfg.model_path(cfg.log_file)}")
        return
    print(f"OK - the 3D case runs. Results: {cfg.model_path(cfg.results3d_slf)}")


if __name__ == "__main__":
    main(run="--run" in sys.argv[1:])
