"""Test-run the built TELEMAC case and check hotstart convergence (workflow step 1, continued).

After ``preprocessing.py`` has assembled the case in ``hydromate-case/simulation/``,
this launches the solver once on exactly that ``steady2d.cas`` to confirm the case
runs without crashing -- it does NOT rebuild anything. This concludes the
preprocessing step. Next run ``mesh_convergence_study.py``, then
hand off to HydroBayesCal.

Because this steady result is also the **hotstart** seed for the HydroBayesCal
calibration, the run then gets a flux / mass-balance convergence analysis (via the
``pythomac`` package). It writes the same four files as pythomac's example into the
simulation folder -- ``extracted-fluxes.csv`` + ``flux-convergence.png`` (per-boundary
fluxes) and ``convergence-rate.csv`` + ``convergence-rate.png`` (the relative flux
imbalance and its convergence rate) -- and reports the simulation time at which the
boundary fluxes reach mass balance to the hotstart tolerance (1e-4; 0.01% imbalance).
When the fluxes stay balanced within 1e-3 m3/s over 10 consecutive listing printouts
(or, on a noisy steady state, in the 10-printout mean), a ``hotstart2d.cas`` is
written next to the steady case: it continues from the steady result ``r2d.slf``
with that steady time as ``DURATION`` and the constant Q / H prescriptions kept
alive. The per-processor ``*_p0000N.sortie`` copies of a parallel run are deleted.
See ``hydromate.flux_convergence``.

Needs ``telemac.pysource`` in case-config.yml to point at a real TELEMAC env, and
``pythomac`` importable (``pip install pythomac`` or set ``PYTHOMAC_DIR`` to a local
checkout).

Run: mamba run -n hydromate-env python cases/<your-case>/initial_run.py
"""

from __future__ import annotations

from pathlib import Path

from hydromate import (
    format_flux_convergence,
    report_sections,
    report_wetting,
    run_solver_streaming,
    setup_logging,
)
from hydromate.config import load_config
from hydromate.env import TelemacRuntime
from hydromate.flux_convergence import HOTSTART_TOLERANCE, analyze_flux_convergence

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

# Number of parallel MPI processes for this test run. None -> use the core
# count assigned in preprocessing (case-config.yml telemac.n_processors);
# set an integer here to override it for this run only (e.g. NCSIZE = 8).
NCSIZE: int | None = None


def main() -> None:
    cas = cfg.model_path(cfg.cas_file)
    if not cas.exists():
        print(f"no built case at {cas}.")
        print("run  python cases/<your-case>/preprocessing.py  first.")
        return

    setup_logging(cfg.model_path(cfg.log_file))   # append to the simulation log
    print(f"test-running the built case: {cas}")
    print("streaming TELEMAC output (simulated-time progress bar below):\n")

    runtime = TelemacRuntime(cfg.telemac)
    try:
        runtime.check_available()
        # stream the solver listing live and show a simulated-time vs DURATION
        # progress bar instead of running silently (see hydromate.progress).
        proc = run_solver_streaming(runtime, cfg, ncsize=NCSIZE)
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
        for line in format_flux_convergence(fc):
            print(line)

    # WHERE the water is. A balanced flux budget says nothing about wetted extent:
    # water seeded above the level the run converges to cannot leave a 2D model (no
    # infiltration, no evaporation) and survives as stagnant film. The report splits
    # the wetted area into active flow / film / isolated puddles, attributes the film
    # to the pre-wet seed, and says whether it is still draining; the outlet profile
    # checks the prescribed downstream stage against the reach's own surface slope.
    for line in report_wetting(cfg):
        print(line)

    # discharge across the geodata.control_sections lines, if the case defines any -
    # how the total Q splits between the threads of a braided reach.
    for line in report_sections(cfg):
        print(line)

    print("next: python cases/<your-case>/mesh_convergence_study.py")


if __name__ == "__main__":
    main()
