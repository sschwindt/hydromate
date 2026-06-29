"""Temporal (flux / mass-balance) convergence analysis of the steady test run.

The ``initial_run.py`` test run serves two purposes: confirm the built case does
not crash, and -- because that same steady result is the **hotstart** seed for the
HydroBayesCal calibration -- find the simulation time at which the model has
reached a steady, mass-conservative state. Calibrating against a not-yet-converged
hotstart would bake a transient bias into every perturbed run.

Convergence criterion (after ``mass-balance-bc.md`` / ``convergence.md`` in the
hyhome numerics notes): a **steady** Telemac2d run is converged when the liquid
boundaries are in mass balance, i.e. total inflow equals total outflow. We track
the dimensionless *relative flux imbalance* per listing printout

    eps_t = | |Q_in,t| - |Q_out,t| | / |Q_in,t|

(magnitudes because Telemac reports inflow positive, outflow negative; normalising
by the inflow makes it dimensionless), and take the run as converged at the first
printout beyond which ``eps_t`` stays permanently below a target tolerance. For a
steady run this mass-flux balance is the governing criterion: once it holds, the
depth and velocity *fields* have stopped evolving as well. (Pointwise depth /
velocity grid-convergence at the measurement probes is a different question,
handled by the mesh-convergence study.)

The default tolerance is 1e-4 (a relative flux imbalance of 0.01% -- the fields have
effectively stopped evolving by then, so it is a sound hotstart seed). The notes also
list a tighter 1e-6 for strict validation, but reaching it costs a very long
simulated time (the imbalance asymptotes slowly), so it is not the default; pass
``tolerance=1e-6`` to ``analyze_flux_convergence`` if you want that grade. The heavy
lifting (parsing the ``.sortie`` listing,
the convergence maths, the plots) is done by the user's **pythomac** package; this
module is the thin hydromate-side adapter that aggregates the per-boundary fluxes,
converts the printout index to a ``NUMBER OF TIME STEPS`` recommendation, and logs
the outcome.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hydromate.config import Config

log = logging.getLogger("hydromate")

# the user's local pythomac checkout (overridable); falls back to a pip install
_DEFAULT_PYTHOMAC = "/home/schwindt/github/pythomac"

# hotstart tolerance for the relative flux imbalance (see module docstring); 1e-4
# (0.01% imbalance) is a well-converged steady state without the slow 1e-6 tail
HOTSTART_TOLERANCE = 1.0e-4


@dataclass
class FluxConvergence:
    """Outcome of the flux/mass-balance convergence analysis."""

    converged: bool
    tolerance: float
    final_imbalance: float                 # last relative flux imbalance eps_t
    converged_time_steps: int | None       # recommended NUMBER OF TIME STEPS (or None)
    converged_seconds: float | None        # the same, in simulation seconds
    listing_printout_period: int
    time_step: float
    fluxes_csv: Path | None
    flux_plot: Path | None
    rate_plot: Path | None


def _import_pythomac(pythomac_dir: str | Path | None):
    """Import pythomac, preferring a pip install, then the local checkout.

    pythomac's parser is pure Python (re/numpy/pandas/matplotlib, no TELEMAC API),
    so it runs fine inside ``hydromate-env``. The local dev checkout is honoured via
    *pythomac_dir* or the ``PYTHOMAC_DIR`` env var, mirroring how ``telemac.pysource``
    is a machine-specific path.
    """
    try:
        import pythomac  # noqa: F401
        from pythomac import (calculate_convergence, extract_fluxes,
                              get_convergence_time)
        return extract_fluxes, calculate_convergence, get_convergence_time
    except ModuleNotFoundError:
        pass

    candidate = Path(pythomac_dir or os.environ.get("PYTHOMAC_DIR", _DEFAULT_PYTHOMAC))
    if not (candidate / "pythomac").is_dir():
        raise ModuleNotFoundError(
            "pythomac is not importable. Install it (`pip install pythomac`) or set "
            f"PYTHOMAC_DIR to a local checkout (looked in {candidate})."
        )
    sys.path.insert(0, str(candidate))
    from pythomac import (calculate_convergence, extract_fluxes,  # type: ignore
                          get_convergence_time)
    return extract_fluxes, calculate_convergence, get_convergence_time


def _gross_in_out(fluxes_df):
    """Aggregate the signed per-boundary flux columns into gross inflow / outflow.

    Telemac reports one signed cumulated flowrate per liquid boundary (inflow
    positive, outflow negative). Summing the positive parts gives the gross inflow
    and the negative parts the gross outflow at each printout, which generalises the
    two-boundary imbalance of the notes to any number of liquid boundaries (the Inn
    case has three: two inflow, one outflow). Leading rows with no inflow yet (the
    dry start, where the imbalance is undefined) are dropped.
    """
    flux_cols = [c for c in fluxes_df.columns if "flux" in str(c).lower()]
    if not flux_cols:
        raise ValueError(
            "no flux columns in the extracted sortie -- did the run use "
            "'PRINTING CUMULATED FLOWRATES : YES' and the -s flag?"
        )
    signed = fluxes_df[flux_cols].to_numpy(dtype=float)
    gross_in = np.where(signed > 0.0, signed, 0.0).sum(axis=1)
    gross_out = np.where(signed < 0.0, -signed, 0.0).sum(axis=1)
    keep = gross_in > 0.0
    return gross_in[keep], gross_out[keep]


def analyze_flux_convergence(
    cfg: Config,
    *,
    tolerance: float = HOTSTART_TOLERANCE,
    pythomac_dir: str | Path | None = None,
) -> FluxConvergence:
    """Analyse boundary-flux convergence of the steady run in ``cfg.model_dir``.

    Reads the latest ``<cas>_<timestamp>.sortie`` listing (so the run must have used
    the ``-s`` flag and ``PRINTING CUMULATED FLOWRATES : YES``), writes
    ``flux-convergence.png`` + ``convergence-rate.png`` + ``extracted-fluxes.csv``
    into ``model_dir``, and returns the converged ``NUMBER OF TIME STEPS`` (the time
    at which the relative flux imbalance drops permanently below *tolerance*).
    """
    extract_fluxes, calculate_convergence, get_convergence_time = _import_pythomac(
        pythomac_dir)

    model_dir = str(cfg.model_dir)
    period = int(cfg.hydrodynamics.listing_printout_period)
    dt = float(cfg.hydrodynamics.time_step)

    log.info("flux-convergence analysis (hotstart tolerance %.0e) on %s",
             tolerance, cfg.cas_file)
    fluxes_df = extract_fluxes(model_directory=model_dir, cas_name=cfg.cas_file,
                               plotting=True)
    if isinstance(fluxes_df, int):  # pythomac returns -1 on failure
        raise RuntimeError(
            "pythomac.extract_fluxes failed to read the sortie listing; check that "
            "the run used '-s' and printed cumulated flowrates.")

    gross_in, gross_out = _gross_in_out(fluxes_df)
    if gross_in.size < 3:
        raise RuntimeError(
            f"only {gross_in.size} flux printout(s) available -- the run is too "
            "short to assess convergence (need several listing printouts).")

    iota_df = calculate_convergence(gross_in, gross_out, cas_timestep=period * dt,
                                    plot_dir=model_dir)
    imbalance = iota_df["Relative imbalance"].to_numpy(dtype=float).real
    final_imbalance = float(imbalance[-1])

    idx = get_convergence_time(imbalance, convergence_precision=tolerance)
    converged = not (idx is None or (isinstance(idx, float) and np.isnan(idx)))

    fluxes_csv = cfg.model_path("extracted-fluxes.csv")
    flux_plot = cfg.model_path("flux-convergence.png")
    rate_plot = cfg.model_path("convergence-rate.png")

    if converged:
        # idx counts listing-printout intervals; one interval = `period` timesteps
        conv_steps = int(idx) * period
        conv_secs = conv_steps * dt
        log.info("  fluxes converged to <%.0e at printout %d -> %d time steps "
                 "(%.0f s); final imbalance %.2e", tolerance, int(idx),
                 conv_steps, conv_secs, final_imbalance)
        log.info("  hotstart recommendation: set NUMBER OF TIME STEPS : %d "
                 "(currently %d)", conv_steps, cfg.hydrodynamics.n_time_steps)
    else:
        conv_steps = conv_secs = None
        log.warning("  fluxes did NOT reach the %.0e imbalance tolerance within the "
                    "run (final imbalance %.2e). Increase NUMBER OF TIME STEPS or "
                    "relax the tolerance.", tolerance, final_imbalance)

    return FluxConvergence(
        converged=converged,
        tolerance=tolerance,
        final_imbalance=final_imbalance,
        converged_time_steps=conv_steps,
        converged_seconds=conv_secs,
        listing_printout_period=period,
        time_step=dt,
        fluxes_csv=fluxes_csv if fluxes_csv.exists() else None,
        flux_plot=flux_plot if flux_plot.exists() else None,
        rate_plot=rate_plot if rate_plot.exists() else None,
    )
