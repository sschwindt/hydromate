"""Build the three TELEMAC-3D cases from the converged 2D run (optional extension).

Runs strictly after the 2D path (``initial_run.py`` -> ``r2d.slf``; settle the
horizontal mesh with ``mesh_convergence_study.py`` first). Delegates everything to
:func:`axqua.build_3d_cases`, which writes exactly three steering files:

1. ``hotstart3d_hydrostatic.cas`` - hydrostatic, constant Q/H, ~30k fixed steps
   with a short listing period: the steady **boundary-flux convergence check**.
2. ``hotstart3d_hydrodyn.cas``    - non-hydrostatic steady run, in-file Q/H.
3. ``unsteady3d.cas``             - non-hydrostatic, hydrograph Q(t) + outflow
   SL(t) via the same liquid-boundaries file as ``unsteady2d.cas`` (skipped with a
   notice unless ``boundaries.inflow`` is a varying series).

All three hotstart from ``r2d.slf`` (v9 2D->3D continuation) and share the vertical
discretisation, turbulence closure and Courant-sized time step inferred from one
read of the 2D result - see ``axqua.threed`` for the how and why.

Pass ``--run [hydrostatic|hydrodyn|unsteady]`` to also launch ``telemac3d.py`` on
one case (default: hydrostatic, the flux-convergence check) with the live listing
+ simulated-time progress bar.

Run: mamba run -n axqua-env python cases/<your-case>/add3d.py [--run [which]]
"""

from __future__ import annotations

import sys
from pathlib import Path

from axqua import (build_3d_cases, format_3d_cases, run_solver_streaming,
                       setup_logging)
from axqua.config import load_config
from axqua.env import TelemacRuntime

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

# Step count / listing spacing of the hydrostatic flux-convergence run. None ->
# axqua defaults (threed.HYDROSTATIC_N_STEPS = 30000, ..._LISTING_PERIOD = 100).
HYDROSTATIC_STEPS: int | None = None
HYDROSTATIC_LISTING: int | None = None


def main(run: str | None = None) -> None:
    if not cfg.model_path(cfg.results_slf).exists():
        print(f"no 2D result at {cfg.model_path(cfg.results_slf)}.")
        print("run  python cases/<your-case>/initial_run.py  first (it produces r2d.slf).")
        return

    setup_logging(cfg.model_path(cfg.log_file))   # append to the simulation log
    print(f"building the TELEMAC-3D cases from {cfg.results_slf}\n")
    setups = build_3d_cases(cfg, hydrostatic_steps=HYDROSTATIC_STEPS,
                            hydrostatic_listing=HYDROSTATIC_LISTING)
    for line in format_3d_cases(setups):
        print(line)

    if run is None:
        print("\nnext: launch one with  python cases/<your-case>/add3d.py --run "
              "[hydrostatic|hydrodyn|unsteady]")
        print("(default: hydrostatic, the flux-convergence check; or run "
              f"telemac3d.py <case>.cas directly in {cfg.model_dir})")
        return

    setup = setups.get(run)
    if setup is None:
        print(f"\ncannot run '{run}': that case was not built (see above).")
        return
    print(f"\nlaunching telemac3d.py on {setup.cas.name} ...")
    print("streaming TELEMAC output (simulated-time progress bar below):\n")
    runtime = TelemacRuntime(cfg.telemac)
    try:
        runtime.check_available()
        # 3D has no DURATION keyword: the bar spans the fixed step count times dt
        proc = run_solver_streaming(runtime, cfg, cas_file=setup.cas.name,
                                    solver="telemac3d",
                                    duration=setup.time_step * setup.n_time_steps)
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(f"could not run telemac3d: {type(exc).__name__}: {exc}")
        return
    if proc.returncode != 0:
        print(f"FAILED - telemac3d returned {proc.returncode}; "
              f"see {cfg.model_path(cfg.log_file)}")
        return
    print(f"OK - {setup.cas.name} runs.")


def _parse_argv(argv: list[str]) -> str | None:
    """``--run [which]`` -> the case to launch (default 'hydrostatic'), or None."""
    if "--run" not in argv:
        return None
    i = argv.index("--run")
    if i + 1 < len(argv) and argv[i + 1] in ("hydrostatic", "hydrodyn", "unsteady"):
        return argv[i + 1]
    return "hydrostatic"


if __name__ == "__main__":
    main(run=_parse_argv(sys.argv[1:]))
