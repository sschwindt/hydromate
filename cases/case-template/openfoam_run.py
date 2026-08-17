"""Run the OpenFOAM free-surface case in two stages (OPTIONAL 3D extension).

After ``openfoam_preprocessing.py`` has assembled the case, this checks the mesh,
decomposes it, runs both stages of ``interFoam``, reconstructs the result and reports
whether the boundary discharges have balanced. It does NOT rebuild anything.

Why two stages
--------------
The first moments after a hotstart are the least like the converged flow: the 2D
result is depth-averaged, so at t = 0 the 3D field has no vertical structure at all,
and the interface is being asked to sharpen while the velocity profile that should
support it does not yet exist. Stage 1 therefore runs briefly at a tight Courant
number with half the interface compression and upwinded momentum - deliberately
robust and deliberately short. Stage 2 restarts from it and runs the production
settings: semi-implicit MULES (which is what lets the Courant target go to ~0.9
instead of the tutorials' 0.2), full interface compression and a limited-linear
momentum scheme.

Reading the output
------------------
The two numbers to watch as it streams are on the progress bar: ``Co`` should sit at
the stage's Courant target, and ``dt`` should hold roughly steady. A run whose air
phase is misbehaving shows ``dt`` falling by orders of magnitude while the simulated
clock barely advances - if that happens, lower ``openfoam.air_velocity_cap``, or
shrink ``openfoam.freeboard`` so there is less air to misbehave.

Afterwards, ``discharge-convergence.csv`` / ``.png`` report the water discharge
through every inlet and outlet patch and their relative imbalance, judged against the
same ``hydrodynamics.flux_tolerance`` the 2D run is judged by - so a 2D and a 3D run
of this reach are held to one standard.

Run: mamba run -n axqua-env python cases/<your-case>/openfoam_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from axqua import setup_logging
from axqua.config import load_config
from axqua.solvers.openfoam import OpenFoamRuntime, report

# optional CLI arg selects the scenario config
CONFIG = Path(__file__).resolve().parent / (
    sys.argv[1] if len(sys.argv) > 1 else "case-config.yml")
cfg = load_config(CONFIG)

# Number of MPI ranks for this run. None -> openfoam.n_processors from the config.
NPROCS: int | None = None
# Set to "run" to skip the spin-up (only sensible when a spin-up has already been
# done and its time directories are still present), or "spinup" to stop after it.
STAGES: tuple[str, ...] = ("spinup", "run")
# Reconstruct the decomposed result afterwards. Off for a quick check: reconstruction
# of a multi-million-cell case takes a while and ParaView can read the decomposed
# case directly.
RECONSTRUCT = True


def main() -> None:
    case_dir = cfg.openfoam_case_dir
    if not (case_dir / "constant" / "polyMesh" / "faces").exists():
        print(f"no built OpenFOAM case at {case_dir}.")
        print("run  python cases/<your-case>/openfoam_preprocessing.py  first.")
        return

    setup_logging(case_dir / cfg.log_file)
    try:
        runtime = OpenFoamRuntime(cfg.openfoam)
        version = runtime.check_available()
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(f"could not reach OpenFOAM: {type(exc).__name__}: {exc}")
        return
    print(f"using {version}; case {case_dir}")

    print("\nchecking the mesh ...")
    usable, failed, _ = runtime.check_mesh(case_dir)
    if not usable:
        print("checkMesh found problems that prevent a run:")
        for line in failed:
            print(f"  {line}")
        print("Fix the mesh first - a solver started on a broken mesh wastes the "
              "whole run.")
        return
    if failed:
        # a mesh cut from a real DEM almost always carries a few marginal faces at
        # survey steps and seams; that is the terrain, not a meshing fault
        print("checkMesh: runnable, with marginal faces reported:")
        for line in failed:
            print(f"  {line}")
    else:
        print("checkMesh: Mesh OK")

    nprocs = NPROCS if NPROCS is not None else cfg.openfoam.n_processors
    if nprocs and nprocs > 1:
        print(f"\ndecomposing over {nprocs} subdomains ...")
        runtime.decompose(case_dir)

    from axqua.solvers.openfoam.dicts import stages

    for stage in stages(cfg):
        if stage.name not in STAGES:
            continue
        print(f"\n=== stage: {stage.name} - {stage.purpose} ===")
        print(f"    to t = {stage.end_time:g} s at Courant {stage.max_courant:g}\n")
        start = 0.0 if stage.name == "spinup" else cfg.openfoam.spinup_time
        # cfg is passed so the stage dictionaries are regenerated from THIS config:
        # editing end_time / max_courant and re-running must actually take effect
        proc = runtime.run_stage(case_dir, stage.name, end_time=stage.end_time,
                                 start_time=start, n_processors=nprocs, cfg=cfg)
        if proc.returncode != 0:
            print(f"\nFAILED - {stage.name} returned {proc.returncode}; "
                  f"see {case_dir / cfg.log_file}")
            return

    if RECONSTRUCT and nprocs and nprocs > 1:
        print("\nreconstructing ...")
        runtime.reconstruct(case_dir)

    print("\n=== boundary discharge balance ===")
    try:
        history, files = report.write_report(cfg, case_dir)
    except Exception as exc:  # noqa: BLE001 - the run already succeeded
        print(f"discharge report skipped: {type(exc).__name__}: {exc}")
        return
    for line in history.lines():
        print(line)
    for path in files:
        print(f"  wrote {path.name}")

    # A balanced discharge says nothing about whether the surface was free to find
    # its own level: the lid and the footprint were both sized from the 2D seed, and
    # a run that pressed against either was constrained by a meshing decision rather
    # than by the flow - which nothing else in the output would reveal.
    print("\n=== was the surface free? ===")
    for line in report.surface_freedom(cfg, case_dir).lines(cfg):
        print(line)
    print(f"\nopen {case_dir / 'case.foam'} in ParaView to view the result "
          "(threshold alpha.water > 0.5 to see the water phase alone).")


if __name__ == "__main__":
    main()
