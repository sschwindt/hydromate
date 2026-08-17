"""Unsteady (hydrograph) run from the converged steady case (optional extension).

Turns the steady case built by ``preprocessing.py`` + ``initial_run.py`` into a
hydrograph-driven **quasi-steady** run, hotstarted from the converged 2D result
``r2d.slf`` (see ``axqua.unsteady``). The flood wave Q(t) comes from THIS case's
``boundaries.inflow`` time series in ``case-config.yml`` (a varying discharge - a
constant Q is the steady case). axqua's generated ``boundaries.cli`` is reused
unchanged: the inflow discharge is driven by the ``LIQUID BOUNDARIES FILE`` and the
downstream water level by its prescribed elevation / rating, so there is no manual
``.cli`` editing.

Prerequisites (run these first):
  1. preprocessing.py   -> builds the case + serializes the liquid-boundary numbering
  2. initial_run.py     -> produces the steady hotstart result r2d.slf
For a 3D unsteady run also settle the horizontal mesh with mesh_convergence_study.py
first (the 3D case reuses that horizontal mesh).

Toggle the run below, then:
  mamba run -n axqua-env python cases/<your-case>/unsteady_run.py [--run]

``--run`` also launches the solver; otherwise the case is only written and the
command to run it is printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from axqua import build_unsteady_3d_case, build_unsteady_case, setup_logging
from axqua.config import load_config
from axqua.env import TelemacRuntime

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

# ---- user options (edit these) ------------------------------------------------
MODE_3D = False           # False -> telemac2d unsteady; True -> telemac3d unsteady
CONTROL_SECTIONS = True   # write CONTROL SECTIONS (per-boundary flux verification; 2D)

# GAIA morphodynamics (bedload / suspended load). Enabling any of these couples GAIA;
# suspended load is transported as TELEMAC tracers through the coupling. Needs
# sediment classes (morphodynamics.sediment_classes) in case-config.yml.
GAIA_ENABLED = False      # master switch for morphodynamics
GAIA_BEDLOAD = True       # BED LOAD FOR ALL SANDS
GAIA_SUSPENDED = True     # SUSPENSION FOR ALL SANDS (suspended load / tracer)
# -------------------------------------------------------------------------------


def _apply_gaia_options() -> None:
    """Fold the in-script GAIA toggles into the loaded config."""
    cfg.morphodynamics.enabled = GAIA_ENABLED
    cfg.morphodynamics.bedload = GAIA_BEDLOAD
    cfg.morphodynamics.suspended_load = GAIA_SUSPENDED


def main(run: bool = False) -> None:
    if not cfg.model_path(cfg.results_slf).exists():
        print(f"no steady result at {cfg.model_path(cfg.results_slf)}.")
        print("run preprocessing.py then initial_run.py first (they produce r2d.slf).")
        return

    setup_logging(cfg.model_path(cfg.log_file))   # append to the simulation log
    _apply_gaia_options()
    solver = "telemac3d" if MODE_3D else "telemac2d"
    print(f"building the unsteady {solver} case (hotstart from {cfg.results_slf})")
    if GAIA_ENABLED:
        modes = ", ".join(m for m, on in (("bedload", GAIA_BEDLOAD),
                                          ("suspended", GAIA_SUSPENDED)) if on) or "none"
        print(f"  GAIA morphodynamics ON ({modes})")

    try:
        if MODE_3D:
            s = build_unsteady_3d_case(cfg)
            cas_name = s.cas.name
            print(f"\nwrote {cas_name}:")
            print(f"  hydrograph  : {s.liquid_boundaries.name} over {s.duration:.0f} s")
            print(f"  vertical    : {s.n_levels} sigma levels (dz~{s.dz:.2f} m)")
            print(f"  time step   : {s.time_step} s ({s.n_time_steps} steps)")
        else:
            s = build_unsteady_case(cfg, control_sections=CONTROL_SECTIONS)
            cas_name = s.cas.name
            print(f"\nwrote {cas_name}:")
            print(f"  hydrograph  : {s.liquid_boundaries.name} over {s.duration:.0f} s "
                  f"({s.n_inflows} inflow / {s.n_outflows} outflow boundaries)")
            print(f"  turbulence  : model {s.turbulence_model}")
            if s.control_sections:
                print(f"  sections    : {s.control_sections.name} "
                      f"(-> {cfg.sections_output_file})")
        if s.gaia_cas:
            print(f"  GAIA        : {s.gaia_cas.name}")
        print(f"  results     : {s.results.name if not MODE_3D else s.results3d.name}")
    except Exception as exc:  # noqa: BLE001 - report what is still missing
        print(f"could not build the unsteady case: {type(exc).__name__}: {exc}")
        return

    if not run:
        print(f"\nnext: run it with  python cases/<your-case>/unsteady_run.py --run")
        print(f"or directly:  {solver}.py {cas_name}  (in {cfg.model_dir})")
        return

    print(f"\nlaunching {solver}.py on {cas_name} ...")
    runtime = TelemacRuntime(cfg.telemac)
    try:
        runtime.check_available()
        proc = runtime.run_solver(cas_name, cwd=cfg.model_dir,
                                  ncsize=cfg.telemac.n_processors, solver=solver)
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(f"could not run {solver}: {type(exc).__name__}: {exc}")
        return
    if proc.returncode != 0:
        print(f"FAILED - {solver} returned {proc.returncode}; "
              f"see {cfg.model_path(cfg.log_file)}")
        return
    print("OK - the unsteady case runs.")


if __name__ == "__main__":
    main(run="--run" in sys.argv[1:])
