"""Temporal (flux / mass-balance) convergence analysis of the steady test run.

The ``initial_run.py`` test run serves two purposes: confirm the built case does
not crash, and -- because that same steady result is the **hotstart** seed for the
HydroBayesCal calibration -- find the simulation time at which the model has
reached a steady, mass-conservative state. Calibrating against a not-yet-converged
hotstart would bake a transient bias into every perturbed run.

Convergence criterion (after ``mass-balance-bc.md`` / ``convergence.md`` in the
hyhome numerics notes): a **steady** Telemac2d run is converged when the liquid
boundaries are in mass balance, i.e. total inflow equals total outflow. We track
the dimensionless *relative flux imbalance* per listing printout::

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

# absolute steady-state criterion for the hotstart end time: the run counts as steady
# from the first listing printout at which the ABSOLUTE flux imbalance
# ||Q_in| - |Q_out|| stays below STEADY_ABS_TOLERANCE (m3/s) for STEADY_WINDOW
# consecutive printouts; that time becomes the DURATION of the generated hotstart case
STEADY_ABS_TOLERANCE = 1.0e-3
STEADY_WINDOW = 10


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
    rate_csv: Path | None
    rate_plot: Path | None
    steady_seconds: float | None = None    # first time of the steady abs-imbalance window
    steady_abs_tolerance: float = STEADY_ABS_TOLERANCE
    steady_window: int = STEADY_WINDOW
    steady_strict: bool = True             # False: window-mean fallback, not per-printout
    hotstart_cas: Path | None = None       # generated hotstart steering file


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
    times = fluxes_df.index.to_numpy(dtype=float)
    return times[keep], gross_in[keep], gross_out[keep]


def find_steady_window(
    times,
    gross_in,
    gross_out,
    *,
    abs_tolerance: float = STEADY_ABS_TOLERANCE,
    window: int = STEADY_WINDOW,
) -> float | None:
    """Simulated time [s] at which the boundary fluxes reach a *sustained* balance.

    Returns the time of the first listing printout whose absolute flux imbalance
    ``||Q_in| - |Q_out||`` is below *abs_tolerance* (m3/s) **and stays below it for
    *window* consecutive printouts** (so a momentary crossing of the oscillating
    imbalance does not count), or ``None`` if no such window exists. This time is
    the end time (``DURATION``) for the generated hotstart case: continuing from
    the steady result, the flow re-equilibrates well within the span the initial
    run needed to balance its fluxes.
    """
    diff = np.abs(np.abs(np.asarray(gross_in, dtype=float))
                  - np.abs(np.asarray(gross_out, dtype=float)))
    ok = diff < abs_tolerance
    for i in range(ok.size - window + 1):
        if ok[i:i + window].all():
            return float(times[i])
    return None


def _mean_balance_time(times, gross_in, gross_out, *, abs_tolerance, window) -> float | None:
    """Fallback steady time when the strict per-printout window never occurs.

    A CFL-marched steady state keeps oscillating around perfect balance (wetting/
    drying + turbulence noise), so the *instantaneous* imbalance can sit above
    *abs_tolerance* at every single printout even though the run is steady. The
    robust statistic is then the imbalance **averaged over the window**: returns the
    time of the first *window*-printout rolling mean of the signed imbalance
    ``Q_in - Q_out`` whose magnitude is below *abs_tolerance* and stays there for the
    next *window* means (so a single lucky window does not count), or ``None``.
    """
    signed = np.abs(np.asarray(gross_in, dtype=float)) - np.abs(
        np.asarray(gross_out, dtype=float))
    if signed.size < 2 * window:
        return None
    roll = np.convolve(signed, np.ones(window) / window, mode="valid")
    ok = np.abs(roll) < abs_tolerance
    for i in range(ok.size - window + 1):
        if ok[i:i + window].all():
            return float(times[i])
    return None


def _remove_processor_sorties(model_dir: Path, cas_name: str) -> int:
    """Delete the per-processor listing copies of a parallel run.

    A parallel run leaves one ``<cas>_<timestamp>_p0000N.sortie`` per MPI rank next
    to the merged main listing; only the main ``.sortie`` carries the flux printouts
    analysed here, so the per-rank copies are just clutter."""
    removed = 0
    for p in sorted(Path(model_dir).glob(f"{cas_name}_*_p*.sortie")):
        p.unlink()
        removed += 1
    return removed


def _plot_relative_imbalance(iota_df, path) -> None:
    """Fallback convergence plot: the relative flux imbalance over time, robust to
    the undefined convergence rate (NaN) of a not-yet-converging run."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    imbalance = iota_df["Relative imbalance"].to_numpy(dtype=float).real
    times = iota_df.index.to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    ax.plot(times, imbalance, "-o", ms=3, color="#1F4E78")
    ax.set_yscale("log")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(r"Relative flux imbalance $\Delta_{Q,t}$")
    ax.set_title("Boundary-flux convergence (relative imbalance)")
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def analyze_flux_convergence(
    cfg: Config,
    *,
    tolerance: float = HOTSTART_TOLERANCE,
    abs_tolerance: float = STEADY_ABS_TOLERANCE,
    steady_window: int = STEADY_WINDOW,
    write_hotstart: bool = True,
    pythomac_dir: str | Path | None = None,
) -> FluxConvergence:
    """Analyse boundary-flux convergence of the steady run in ``cfg.model_dir``.

    Reads the latest ``<cas>_<timestamp>.sortie`` listing (so the run must have used
    the ``-s`` flag and ``PRINTING CUMULATED FLOWRATES : YES``) and writes the same
    four files as pythomac's worked example into ``model_dir`` -
    ``extracted-fluxes.csv`` + ``flux-convergence.png`` (the per-boundary fluxes) and
    ``convergence-rate.csv`` + ``convergence-rate.png`` (the relative imbalance and
    convergence rate). Returns the converged ``NUMBER OF TIME STEPS`` (the time at
    which the relative flux imbalance drops permanently below *tolerance*).

    On top of the relative-imbalance criterion, the *absolute* steady window is
    detected (``find_steady_window``: ``||Q_in|-|Q_out|| < abs_tolerance`` m3/s over
    *steady_window* consecutive printouts); when found and *write_hotstart* is on, a
    ``hotstart2d.cas`` continuing from the steady results file with that time as
    ``DURATION`` is written next to the steady case (``steering.write_hotstart_cas``).
    The per-processor ``*_p0000N.sortie`` copies of a parallel run are deleted after
    a successful extraction (only the merged main listing is analysed and kept).
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

    removed = _remove_processor_sorties(Path(model_dir), cfg.cas_file)
    if removed:
        log.info("  removed %d per-processor .sortie file(s); kept the main listing",
                 removed)

    kept_times, gross_in, gross_out = _gross_in_out(fluxes_df)
    if gross_in.size < 3:
        raise RuntimeError(
            f"only {gross_in.size} flux printout(s) available -- the run is too "
            "short to assess convergence (need several listing printouts).")

    # mean simulated time between listing printouts. The run uses a VARIABLE time
    # step, so `period * time_step` is wrong (time_step is only the initial/max); the
    # actual spacing is back-calculated from the sortie's time index, as pythomac's
    # own example does, so the convergence x-axis is in real seconds.
    times = fluxes_df.index.to_numpy(dtype=float)
    cas_timestep = (float(times[-1]) / max(len(times) - 1, 1)
                    if times.size > 1 else period * dt)

    fluxes_csv = cfg.model_path("extracted-fluxes.csv")
    flux_plot = cfg.model_path("flux-convergence.png")
    rate_csv = cfg.model_path("convergence-rate.csv")
    rate_plot = cfg.model_path("convergence-rate.png")

    # convergence rate iota = log_{Delta_t}(Delta_{t+1}); the relative imbalance is
    # the part we judge convergence on. Compute the table without plotting first (so
    # we always get it), then try pythomac's plot. While the run is still filling /
    # in the transient phase the imbalance is flat (~1) so iota is undefined (0/0) and
    # pythomac's plot crashes on the all-NaN rate; fall back to a direct imbalance plot.
    iota_df = calculate_convergence(gross_in, gross_out, cas_timestep=cas_timestep,
                                    plot_dir=None)
    iota_df.to_csv(rate_csv)
    try:
        calculate_convergence(gross_in, gross_out, cas_timestep=cas_timestep,
                              plot_dir=model_dir)
    except Exception as exc:  # noqa: BLE001 - pythomac plot is fragile on flat data
        log.info("  convergence rate not yet defined (%s) - the run is still in the "
                 "filling/transient phase; plotting the relative imbalance directly.",
                 type(exc).__name__)
        _plot_relative_imbalance(iota_df, rate_plot)

    imbalance = iota_df["Relative imbalance"].to_numpy(dtype=float).real
    final_imbalance = float(imbalance[-1])

    idx = get_convergence_time(imbalance, convergence_precision=tolerance)
    converged = not (idx is None or (isinstance(idx, float) and np.isnan(idx)))

    if converged:
        # idx counts listing-printout intervals. The converged *time* is idx ×
        # cas_timestep (the real mean spacing back-calculated above); reporting steps
        # too is only a nominal estimate (the actual dt is CFL-variable).
        conv_secs = float(idx) * cas_timestep
        conv_steps = int(idx) * period
        log.info("  fluxes converged to <%.0e at printout %d -> %.0f s simulated "
                 "(~%d listing iterations); final imbalance %.2e", tolerance, int(idx),
                 conv_secs, conv_steps, final_imbalance)
        log.info("  the steady-state auto-stop ends the run here; as a manual cap use "
                 "DURATION : %.0f (or NUMBER OF TIME STEPS for a fixed-step run).",
                 conv_secs)
    else:
        conv_steps = conv_secs = None
        log.warning("  fluxes did NOT reach the %.0e imbalance tolerance within the "
                    "run (final imbalance %.2e). Increase NUMBER OF TIME STEPS or "
                    "relax the tolerance.", tolerance, final_imbalance)

    # absolute steady window -> hotstart end time + generated hotstart2d.cas
    steady_secs = find_steady_window(kept_times, gross_in, gross_out,
                                     abs_tolerance=abs_tolerance, window=steady_window)
    steady_strict = steady_secs is not None
    if not steady_strict:
        steady_secs = _mean_balance_time(kept_times, gross_in, gross_out,
                                         abs_tolerance=abs_tolerance,
                                         window=steady_window)
        if steady_secs is not None:
            log.warning("  the instantaneous flux imbalance never stayed below %.0e "
                        "m3/s for %d consecutive printouts (steady-state noise floor "
                        "above the tolerance); falling back to the %d-printout MEAN "
                        "balance, first sustained at %.1f s.",
                        abs_tolerance, steady_window, steady_window, steady_secs)
    hotstart_cas = None
    if steady_secs is None:
        log.warning("  the absolute flux imbalance never stayed below %.0e m3/s for "
                    "%d consecutive printouts - no hotstart end time; extend the run "
                    "or relax abs_tolerance.", abs_tolerance, steady_window)
    else:
        if steady_strict:
            log.info("  sustained flux balance (<%.0e m3/s over %d printouts) from "
                     "%.1f s simulated", abs_tolerance, steady_window, steady_secs)
        if write_hotstart:
            from hydromate.steering import write_hotstart_cas
            try:
                hotstart_cas = write_hotstart_cas(cfg, duration=steady_secs)
            except Exception as exc:  # noqa: BLE001 - keep the analysis result usable
                log.warning("  could not write the hotstart steering file: %s: %s",
                            type(exc).__name__, exc)

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
        rate_csv=rate_csv if rate_csv.exists() else None,
        rate_plot=rate_plot if rate_plot.exists() else None,
        steady_seconds=steady_secs,
        steady_abs_tolerance=abs_tolerance,
        steady_window=steady_window,
        steady_strict=steady_strict,
        hotstart_cas=hotstart_cas,
    )
