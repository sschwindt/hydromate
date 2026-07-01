"""Shared workflow helpers for the per-case scripts.

The per-case ``preprocessing.py`` / ``initial_run.py`` / ``mesh_convergence_study.py``
are deliberately thin: the logic they have in common lives here so it is written
once and stays identical across every case (template, example-Inn, ering-revised, ...).

The central rule is that the **steady discharge and the outflow stage prescription
come from the case's own ``case-config.yml``**, never from a constant hard-coded in
a script. :func:`prepare_steady_inputs` reads ``boundaries.prescribed_flowrate``
(or the inflow series) so each case uses its own configured boundary conditions; an
explicit ``discharge=`` only overrides it when a script genuinely needs to.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hydromate.config import Config
from hydromate.env import TelemacRuntime
from hydromate.progress import SolverProgress
from hydromate.rating import synthesize_outflow_rating

log = logging.getLogger("hydromate")

# trapezoidal channel banks (H:V) used when a stage-discharge rating is synthesised
DEFAULT_BANK_SLOPE = 1.0

_UNSET = object()   # sentinel: "leave initialization.prewet_depth untouched"


def resolve_discharge(cfg: Config, discharge: float | None = None) -> float:
    """Steady discharge [m3/s] the case is built for.

    Resolution order: an explicit *discharge* argument, then the case config's
    ``boundaries.prescribed_flowrate`` (the inflow Q set in ``case-config.yml``),
    then the mean of the inflow series in ``boundaries.inflow``. Raises when none of
    these is available - so a case never silently inherits another case's discharge.
    """
    if discharge is not None:
        return float(discharge)
    if cfg.boundaries.prescribed_flowrate is not None:
        return float(cfg.boundaries.prescribed_flowrate)
    inflow = cfg.boundaries.inflow
    if inflow is not None and Path(inflow).exists():
        from hydromate import hydraulics
        return float(hydraulics.read_inflow(Path(inflow), steady=True).steady_value)
    raise ValueError(
        "no steady discharge for the build: set boundaries.prescribed_flowrate "
        "(m3/s) in case-config.yml, provide boundaries.inflow, or pass discharge=..."
    )


def synthesize_constant_inflow(cfg: Config, discharge: float) -> Path:
    """Write a tiny constant-Q inflow series when ``boundaries.inflow`` is missing,
    point the config at it, and return its path (the existing inflow otherwise)."""
    inflow = cfg.boundaries.inflow
    if inflow is not None and Path(inflow).exists():
        return Path(inflow)
    path = cfg.preprocessing_path("inflow-constant.csv")
    path.write_text(
        f"datetime,Q\n2020-11-01 00:00,{discharge}\n2020-11-01 01:00,{discharge}\n"
    )
    cfg.boundaries.inflow = path
    log.info("synthesised constant inflow Q=%g m3/s -> %s", discharge, path.name)
    return path


def synthesize_rating_if_missing(cfg: Config, discharge: float,
                                 side_slope: float = DEFAULT_BANK_SLOPE) -> Path | None:
    """Synthesise the outflow stage-discharge rating from the geodata when the
    ``stage_discharge`` outflow condition is active and no rating CSV is given.

    Width comes from the outflow boundary line, bed + reach slope from the DEM and
    roughness from ``friction.boundary_*`` (see :func:`synthesize_outflow_rating`).
    Returns the rating path (existing or generated), or None for the other outflow
    conditions (``elevation`` / ``free``), which need no rating curve.
    """
    if cfg.boundaries.outflow_condition != "stage_discharge":
        return None
    sd = cfg.boundaries.stage_discharge
    if sd is not None and Path(sd).exists():
        return Path(sd)
    path = synthesize_outflow_rating(cfg, discharge, side_slope=side_slope)
    cfg.boundaries.stage_discharge = path
    log.info("generated outflow rating curve at Q=%g m3/s -> %s", discharge, path)
    return path


def prepare_steady_inputs(cfg: Config, discharge: float | None = None, *,
                          side_slope: float = DEFAULT_BANK_SLOPE,
                          prewet_depth: float | None = _UNSET) -> float:  # type: ignore[assignment]
    """Configure the steady boundary conditions for a build and return the discharge.

    Prescribes the steady discharge (from the config, see :func:`resolve_discharge`),
    synthesises a constant inflow series and the outflow stage-discharge rating when
    those inputs are missing, so the build draws the inflow Q (m3/s) and the outflow
    stage (H) straight from ``case-config.yml``. Used by ``preprocessing.py`` and the
    mesh-convergence study so they share one source of truth for the steady setup.

    *prewet_depth* defaults to the sentinel "leave it as the config has it"; pass a
    float (or None) to set ``initialization.prewet_depth`` - the mesh-convergence study
    hotstarts the channel this way, the dry-start initial run leaves it untouched.
    """
    q = resolve_discharge(cfg, discharge)
    cfg.boundaries.prescribed_flowrate = q
    if prewet_depth is not _UNSET:
        cfg.initialization.prewet_depth = prewet_depth
    synthesize_constant_inflow(cfg, q)
    synthesize_rating_if_missing(cfg, q, side_slope)
    return q


def format_flux_convergence(fc) -> list[str]:
    """Render a :class:`~hydromate.flux_convergence.FluxConvergence` result as
    user-facing report lines (whether the boundary fluxes reached mass balance, the
    hotstart time-step recommendation, and the produced csv/png paths).

    Kept here so every case's ``initial_run.py`` prints the convergence summary the
    same way; the analysis itself is the pythomac binding in
    :mod:`hydromate.flux_convergence`.
    """
    lines: list[str] = []
    if fc.converged:
        lines.append(
            f"fluxes converged (<{fc.tolerance:.0e}) after {fc.converged_time_steps} "
            f"time steps ({fc.converged_seconds:.0f} s); "
            f"final imbalance {fc.final_imbalance:.2e}"
        )
        lines.append(f"  -> hotstart: set NUMBER OF TIME STEPS : {fc.converged_time_steps}")
    else:
        lines.append(
            f"fluxes did NOT reach {fc.tolerance:.0e} (final imbalance "
            f"{fc.final_imbalance:.2e}); extend the run (DURATION / NUMBER OF TIME STEPS)."
        )
    for label, p in (("fluxes csv", fc.fluxes_csv), ("flux plot", fc.flux_plot),
                     ("rate csv", fc.rate_csv), ("rate plot", fc.rate_plot)):
        if p:
            lines.append(f"  {label}: {p}")
    return lines


def expected_duration(cfg: Config) -> float:
    """Total simulated time [s] the run marches to when no stop criterion fires.

    Mirrors :mod:`hydromate.steering`'s ``DURATION`` logic: the explicit
    ``hydrodynamics.duration`` when set, else the ``n_time_steps * time_step``
    fallback. Used to scale the solver progress bar (the variable time step makes
    the *number* of steps unknown a priori, so progress is measured against the
    simulated-time cap instead).
    """
    h = cfg.hydrodynamics
    if h.duration is not None:
        return float(h.duration)
    return float(h.n_time_steps * h.time_step)


def run_solver_streaming(runtime: TelemacRuntime, cfg: Config, *,
                         cas_file: str | None = None,
                         solver: str | None = None,
                         ncsize: int | None = None,
                         show_progress: bool = True):
    """Run the TELEMAC solver, echoing its listing live with a progress bar.

    Unlike a plain :meth:`TelemacRuntime.run_solver`, this streams the solver's
    stdout/stderr to the console as the run marches (so it is not silent) and, when
    *show_progress*, overlays a simulated-time-vs-:func:`expected_duration` progress
    bar parsed from TELEMAC's listing header. Returns the ``CompletedProcess``
    (its ``returncode`` reports success/failure; a non-zero exit does not raise).
    """
    cas_file = cas_file or cfg.cas_file
    if ncsize is None:
        ncsize = cfg.telemac.n_processors
    progress = SolverProgress(expected_duration(cfg)) if show_progress else None
    on_line = progress.feed if progress else print
    try:
        return runtime.run_solver(cas_file, cwd=cfg.model_dir, ncsize=ncsize,
                                  solver=solver, check=False, on_line=on_line)
    finally:
        if progress is not None:
            progress.close()
