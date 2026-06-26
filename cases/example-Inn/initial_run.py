"""Test-run the built TELEMAC case and check hotstart convergence (workflow step 1, continued).

After ``preprocessing.py`` has assembled the case in ``tm-simulation/simulation/``,
this launches the solver once on exactly that ``steady2d.cas`` to confirm the case
runs without crashing -- it does NOT rebuild anything. This concludes the
preprocessing step. Next run ``mesh_convergence_study.py``, then
hand off to HydroBayesCal.

Because this steady result is also the **hotstart** seed for the HydroBayesCal
calibration, the run then gets a flux / mass-balance convergence analysis (via the
``pythomac`` package): it finds the simulation time at which the boundary fluxes
reach mass balance to a tight hotstart tolerance (1e-6), writes ``flux-convergence.png``
and ``convergence-rate.png`` into the simulation folder, and recommends the
``NUMBER OF TIME STEPS`` to use for the hotstart so the calibration is not seeded
with a transient. See ``hydromate.flux_convergence`` for the criterion.

Needs ``telemac.pysource`` in case-config.yml to point at a real TELEMAC env, and
``pythomac`` importable (``pip install pythomac`` or set ``PYTHOMAC_DIR`` to a local
checkout; defaults to ``/home/schwindt/github/pythomac``).

Run: mamba run -n hydromate-env python cases/example-Inn/initial_run.py
"""

from __future__ import annotations

from pathlib import Path

from hydromate import setup_logging
from hydromate.config import load_config
from hydromate.env import TelemacRuntime
from hydromate.flux_convergence import HOTSTART_TOLERANCE, analyze_flux_convergence

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)


def main() -> None:
    cas = cfg.model_path(cfg.cas_file)
    if not cas.exists():
        print(f"no built case at {cas}.")
        print("run  python cases/example-Inn/preprocessing.py  first.")
        return

    setup_logging(cfg.model_path(cfg.log_file))   # append to the simulation log
    print(f"test-running the built case: {cas}")

    runtime = TelemacRuntime(cfg.telemac)
    try:
        runtime.check_available()
        proc = runtime.run_solver(cfg.cas_file, cwd=cfg.model_dir,
                                  ncsize=cfg.telemac.n_processors)
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(f"could not run the solver: {type(exc).__name__}: {exc}")
        return

    if proc.returncode != 0:
        print(f"FAILED - solver returned {proc.returncode}; "
              f"see {cfg.model_path(cfg.log_file)}")
        return

    print(f"OK - the case runs. Results: {cfg.model_path(cfg.results_slf)}")

    # hotstart convergence: when did the boundary fluxes reach mass balance?
    try:
        fc = analyze_flux_convergence(cfg, tolerance=HOTSTART_TOLERANCE)
    except Exception as exc:  # noqa: BLE001 - the run already succeeded; report cleanly
        print(f"convergence analysis skipped: {type(exc).__name__}: {exc}")
    else:
        if fc.converged:
            print(f"fluxes converged (<{fc.tolerance:.0e}) after "
                  f"{fc.converged_time_steps} time steps ({fc.converged_seconds:.0f} s); "
                  f"final imbalance {fc.final_imbalance:.2e}")
            print(f"  -> hotstart: set NUMBER OF TIME STEPS : {fc.converged_time_steps}")
        else:
            print(f"fluxes did NOT reach {fc.tolerance:.0e} (final imbalance "
                  f"{fc.final_imbalance:.2e}); extend NUMBER OF TIME STEPS.")
        for label, p in (("flux plot", fc.flux_plot), ("rate plot", fc.rate_plot)):
            if p:
                print(f"  {label}: {p}")

    print("next: python cases/example-Inn/mesh_convergence_study.py")


if __name__ == "__main__":
    main()
