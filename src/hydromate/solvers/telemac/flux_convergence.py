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

**The tolerance depends on what the result is for**, and the imbalance being a
*relative* measure is what makes that judgement possible: the discharge, depth and
velocity read off a steady run inherit it directly. ``1e-3`` (0.1%) is therefore the
default -- a tenth of a percent moves Q, h or U by far less than the uncertainty of
the field data they are compared against, and by far less than the 5% the
mesh-convergence study accepts as grid-independent. The stricter ``1e-4`` is reserved
for a **hotstart seed**, where a residual transient is not averaged out but inherited
by every one of the dozens of perturbed calibration runs restarted from it; ``1e-6``
is a validation grade whose slow asymptote is rarely worth the simulated time.
Set it per case with ``hydrodynamics.flux_tolerance``, or pass ``tolerance=`` to
:func:`analyze_flux_convergence`.

This module is **self-contained**: the listing is parsed by :mod:`hydromate.solvers.telemac.sortie`
and the convergence maths and plots are below. It previously delegated to the
``pythomac`` package, whose analysis this reproduces (and whose four output files it
still writes, with the same names, so existing workflows and documentation are
unaffected). pythomac is GPL-3 and derives its listing parser from TELEMAC's own
GPL-3 ``postel`` code, which cannot be vendored into BSD-3-licensed hydromate; and
having the maths here removes a set of adapters that existed only to work around the
package boundary -- the printout spacing had to be back-calculated because a
CFL-adaptive run has no fixed time step, the rate plot had to be replaced whenever
the rate was still undefined, and ``convergence-rate.csv`` had to be written
separately because only the ``.png`` was produced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hydromate.config import Config
from hydromate.solvers.telemac.sortie import Sortie, latest_sortie, processor_sorties, read_sortie

log = logging.getLogger("hydromate")

# Tolerance grades for the relative flux imbalance (see the module docstring).
#
# Which one applies depends on WHAT the result is used for, because the imbalance is
# a *relative* measure and the quantities read off a steady run inherit it directly:
#
#   1e-3  HYDRAULIC_TOLERANCE - the default. Adequate whenever the result is read as
#         discharge, water depth or velocity: a 0.1% flux imbalance moves those by
#         far less than the measurement uncertainty of any field campaign (a
#         FlowTracker vertical is good to a few percent), and well below the
#         discretisation error the mesh-convergence study accepts (5%).
#   1e-4  HOTSTART_TOLERANCE - the strict grade for a hotstart *seed*. A calibration
#         fleet restarts from this state a few dozen times, so any residual
#         transient is inherited by every perturbed run rather than averaged out.
#   1e-6  validation grade; reaching it costs a very long simulated time (the
#         imbalance asymptotes slowly) and is rarely worth it.
HYDRAULIC_TOLERANCE = 1.0e-3
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


# --------------------------------------------------------------------------- #
# convergence maths
# --------------------------------------------------------------------------- #


def relative_imbalance(gross_in, gross_out) -> np.ndarray:
    """Relative flux imbalance ``eps_t = ||Q_in| - |Q_out|| / |Q_in|`` per printout.

    Telemac reports boundary fluxes signed (inflow positive, outflow negative), so
    the *magnitudes* are differenced: balance means ``|Q_in| == |Q_out|``, hence
    ``eps_t -> 0`` at steady state. Using magnitudes also keeps the metric robust to
    a boundary that momentarily reverses.
    """
    q_in = np.abs(np.asarray(gross_in, dtype=float))
    q_out = np.abs(np.asarray(gross_out, dtype=float))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.abs(q_in - q_out) / q_in


def convergence_rate(imbalance) -> np.ndarray:
    """Convergence rate ``iota_t = log_{eps_t}(eps_{t+1})``, aligned with ``eps[1:]``.

    A rate of 1 means the imbalance is shrinking at a constant order; above 1 it is
    accelerating. It is **undefined wherever the imbalance is flat or not yet
    decaying** (``log`` of a base near 1, or of a non-positive value), which is the
    normal state during the filling/transient phase - those entries come back as NaN
    rather than raising, and the plots below simply show gaps there.
    """
    eps = np.asarray(imbalance, dtype=float)
    if eps.size < 2:
        return np.zeros(0)
    base, value = eps[:-1], eps[1:]
    out = np.full(base.shape, np.nan)
    ok = (base > 0) & (value > 0) & (np.abs(base - 1.0) > 1e-12)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[ok] = np.log(value[ok]) / np.log(base[ok])
    return out


def convergence_index(imbalance, tolerance: float = HOTSTART_TOLERANCE) -> int | None:
    """First index beyond which *imbalance* stays **permanently** below *tolerance*.

    "Permanently" matters: a transient dip below the tolerance during the filling
    phase is not convergence. Returns ``None`` when the tolerance is never reached,
    or is reached but later lost again.
    """
    eps = np.asarray(imbalance, dtype=float)
    below = eps < tolerance
    if not below.any() or not below[-1]:
        return None
    above = np.flatnonzero(~below)
    return int(above[-1] + 1) if above.size else 0


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


# --------------------------------------------------------------------------- #
# plots
# --------------------------------------------------------------------------- #


def _new_axes(figsize=(7.2, 4.3)):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt, *plt.subplots(figsize=figsize)


def plot_boundary_fluxes(sortie: Sortie, path: Path) -> Path:
    """``flux-convergence.png``: the signed flux at every liquid boundary over time,
    with the gross inflow/outflow totals the convergence metric is built from."""
    plt, fig, ax = _new_axes()
    for i in range(sortie.n_boundaries):
        ax.plot(sortie.time, sortie.fluxes[:, i], lw=1.0, alpha=0.85,
                label=f"boundary {i + 1}")
    ax.plot(sortie.time, sortie.gross_in, "--", lw=1.6, color="#1F4E78",
            label="gross inflow")
    ax.plot(sortie.time, -sortie.gross_out, "--", lw=1.6, color="#A6300F",
            label="gross outflow")
    ax.axhline(0.0, color="0.6", lw=0.8)
    ax.set_xlabel("Simulated time (s)")
    ax.set_ylabel("Flux (m$^3$/s)")
    ax.set_title("Boundary fluxes")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_convergence(times, imbalance, rate, path: Path, *,
                     tolerance: float = HOTSTART_TOLERANCE) -> Path:
    """``convergence-rate.png``: the relative flux imbalance (log scale, with the
    tolerance marked) and, where it is defined, the convergence rate beneath it.

    Drawing the imbalance unconditionally is the point: during the filling phase the
    rate is undefined everywhere, and a rate-only figure would then be empty exactly
    when one most wants to see how far from balance the run still is.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(7.2, 6.0), sharex=True)
    eps = np.asarray(imbalance, dtype=float)
    top.plot(times, eps, "-o", ms=2.5, color="#1F4E78")
    top.axhline(tolerance, color="#A6300F", ls="--", lw=1.0,
                label=f"tolerance {tolerance:.0e}")
    if np.any(eps > 0):
        top.set_yscale("log")
    top.set_ylabel(r"Relative imbalance $\Delta_{Q,t}$")
    top.set_title("Boundary-flux convergence")
    top.grid(True, which="both", alpha=0.3)
    top.legend(fontsize=8)

    rate = np.asarray(rate, dtype=float)
    finite = np.isfinite(rate)
    if finite.any():
        bottom.plot(np.asarray(times, dtype=float)[1:][finite], rate[finite],
                    "-o", ms=2.5, color="#2E6E4E")
        bottom.axhline(1.0, color="0.5", ls="--", lw=1.0)
    else:
        bottom.text(0.5, 0.5, "convergence rate not yet defined\n"
                              "(imbalance still flat - filling/transient phase)",
                    ha="center", va="center", transform=bottom.transAxes,
                    fontsize=9, color="0.35")
    bottom.set_xlabel("Simulated time (s)")
    bottom.set_ylabel(r"Convergence rate $\iota_t$")
    bottom.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# the analysis
# --------------------------------------------------------------------------- #


def _remove_processor_sorties(model_dir: Path, cas_name: str) -> int:
    """Delete the per-processor listing copies of a parallel run.

    A parallel run leaves one ``<cas>_<timestamp>_p0000N.sortie`` per MPI rank next
    to the merged main listing; only the main ``.sortie`` carries the flux printouts
    analysed here, so the per-rank copies are just clutter."""
    removed = 0
    for p in processor_sorties(model_dir, cas_name):
        p.unlink()
        removed += 1
    return removed


def analyze_flux_convergence(
    cfg: Config,
    *,
    tolerance: float | None = None,
    abs_tolerance: float | None = None,
    steady_window: int = STEADY_WINDOW,
    write_hotstart: bool = True,
    pythomac_dir: str | Path | None = None,
) -> FluxConvergence:
    """Analyse boundary-flux convergence of the steady run in ``cfg.model_dir``.

    Reads the latest ``<cas>_<timestamp>.sortie`` listing (so the run must have used
    the ``-s`` flag and ``PRINTING CUMULATED FLOWRATES : YES``) and writes four files
    into ``model_dir`` - ``extracted-fluxes.csv`` + ``flux-convergence.png`` (the
    per-boundary fluxes) and ``convergence-rate.csv`` + ``convergence-rate.png`` (the
    relative imbalance and convergence rate). Returns the converged ``NUMBER OF TIME
    STEPS`` (the time at which the relative flux imbalance drops permanently below
    *tolerance*).

    On top of the relative-imbalance criterion, the *absolute* steady window is
    detected (``find_steady_window``: ``||Q_in|-|Q_out|| < abs_tolerance`` m3/s over
    *steady_window* consecutive printouts); when found and *write_hotstart* is on, a
    ``hotstart2d.cas`` continuing from the steady results file with that time as
    ``DURATION`` is written next to the steady case (``steering.write_hotstart_cas``).
    The per-processor ``*_p0000N.sortie`` copies of a parallel run are deleted after
    a successful extraction (only the merged main listing is analysed and kept).

    *pythomac_dir* is accepted and ignored; the analysis no longer shells out to that
    package (see the module docstring).
    """
    if pythomac_dir is not None:
        log.debug("pythomac_dir=%s ignored: the flux analysis is now built in",
                  pythomac_dir)

    if tolerance is None:
        tolerance = float(getattr(cfg.hydrodynamics, "flux_tolerance",
                                  HYDRAULIC_TOLERANCE))
    if abs_tolerance is None:
        # keep the absolute criterion consistent with the relative one: the same
        # fraction of the discharge actually being routed. A fixed 1e-3 m3/s means
        # something quite different on a 2 m3/s side channel than on a 200 m3/s
        # river, and the hotstart would be gated by the case size rather than by
        # how steady the run is.
        discharge = getattr(cfg.boundaries, "prescribed_flowrate", None) \
            if hasattr(cfg, "boundaries") else None
        abs_tolerance = (float(tolerance) * float(discharge)
                         if discharge else STEADY_ABS_TOLERANCE)

    model_dir = Path(cfg.model_dir)
    period = int(cfg.hydrodynamics.listing_printout_period)
    dt = float(cfg.hydrodynamics.time_step)

    log.info("flux-convergence analysis (relative tolerance %.0e, absolute %.1e m3/s) "
             "on %s", tolerance, abs_tolerance, cfg.cas_file)
    listing = latest_sortie(model_dir, cfg.cas_file)
    if listing is None:
        raise FileNotFoundError(
            f"no '{cfg.cas_file}_<timestamp>.sortie' listing in {model_dir}: run the "
            "solver with the -s flag so it writes one.")
    sortie = read_sortie(listing)
    log.info("  %s: study %r, %d liquid boundaries, %d printouts over %.0f s "
             "(%.1f s apart), solver time %.0f s", listing.name, sortie.study,
             sortie.n_boundaries, sortie.time.size, float(sortie.time[-1]),
             sortie.printout_interval, sortie.exec_seconds)

    fluxes_csv = cfg.model_path("extracted-fluxes.csv")
    flux_plot = cfg.model_path("flux-convergence.png")
    rate_csv = cfg.model_path("convergence-rate.csv")
    rate_plot = cfg.model_path("convergence-rate.png")

    frame = sortie.to_frame()
    frame.to_csv(fluxes_csv)
    plot_boundary_fluxes(sortie, flux_plot)

    removed = _remove_processor_sorties(model_dir, cfg.cas_file)
    if removed:
        log.info("  removed %d per-processor .sortie file(s); kept the main listing",
                 removed)

    # printouts before any inflow has established (a dry start) have an undefined
    # imbalance - drop them rather than dividing by zero
    keep = sortie.gross_in > 0.0
    times = sortie.time[keep]
    iterations = sortie.iteration[keep]
    gross_in, gross_out = sortie.gross_in[keep], sortie.gross_out[keep]
    if gross_in.size < 3:
        raise RuntimeError(
            f"only {gross_in.size} usable flux printout(s) -- the run is too short to "
            "assess convergence (need several listing printouts with inflow).")

    imbalance = relative_imbalance(gross_in, gross_out)
    rate = convergence_rate(imbalance)
    final_imbalance = float(imbalance[-1])

    import pandas as pd
    rates = pd.DataFrame(
        {"Relative imbalance": imbalance,
         "Convergence rate": np.concatenate([[np.nan], rate])},
        index=pd.Index(times, name="time (s)"))
    rates.to_csv(rate_csv)
    plot_convergence(times, imbalance, rate, rate_plot, tolerance=tolerance)

    idx = convergence_index(imbalance, tolerance)
    converged = idx is not None
    if converged:
        # the converged time and iteration count are read straight off the listing at
        # that printout - not estimated from a nominal spacing, which a CFL-adaptive
        # run does not have
        conv_secs = float(times[idx])
        conv_steps = int(iterations[idx])
        log.info("  fluxes converged to <%.0e at printout %d -> %.0f s simulated "
                 "(solver iteration %d); final imbalance %.2e",
                 tolerance, idx, conv_secs, conv_steps, final_imbalance)
        log.info("  as a manual cap use DURATION : %.0f (or NUMBER OF TIME STEPS : %d "
                 "for a fixed-step run).", conv_secs, conv_steps)
    else:
        conv_steps = conv_secs = None
        log.warning("  fluxes did NOT reach the %.0e imbalance tolerance within the "
                    "run (final imbalance %.2e). Increase DURATION or relax the "
                    "tolerance.", tolerance, final_imbalance)

    # absolute steady window -> hotstart end time + generated hotstart2d.cas
    steady_secs = find_steady_window(times, gross_in, gross_out,
                                     abs_tolerance=abs_tolerance, window=steady_window)
    steady_strict = steady_secs is not None
    if not steady_strict:
        steady_secs = _mean_balance_time(times, gross_in, gross_out,
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
            from hydromate.solvers.telemac.steering import write_hotstart_cas
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
