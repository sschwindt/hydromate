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
    method = getattr(cfg.boundaries, "rating_method", "trapezoid")
    if sd is not None and Path(sd).exists():
        return Path(sd)
    out = Path(sd) if sd is not None else cfg.preprocessing_path("rating-curve.csv")
    if method == "section":
        from hydromate.rating import synthesize_outflow_rating_from_section
        path = synthesize_outflow_rating_from_section(cfg, discharge, out=out)
    else:
        path = synthesize_outflow_rating(cfg, discharge, side_slope=side_slope, out=out)
    cfg.boundaries.stage_discharge = path
    log.info("generated outflow rating curve (%s) at Q=%g m3/s -> %s",
             method, discharge, path)
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
    same way; the analysis itself lives in
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
    if fc.steady_seconds is not None:
        basis = ("per-printout" if fc.steady_strict
                 else f"{fc.steady_window}-printout mean; instantaneous criterion "
                      "not met (steady-state noise floor)")
        lines.append(
            f"sustained flux balance (abs imbalance < {fc.steady_abs_tolerance:.0e} m3/s "
            f"over {fc.steady_window} printouts, {basis}) from "
            f"{fc.steady_seconds:.1f} s simulated"
        )
        if fc.hotstart_cas:
            lines.append(f"  -> hotstart case: {fc.hotstart_cas}")
    else:
        lines.append(
            f"no sustained flux balance (abs imbalance < {fc.steady_abs_tolerance:.0e} "
            f"m3/s over {fc.steady_window} printouts) - no hotstart case written."
        )
    for label, p in (("fluxes csv", fc.fluxes_csv), ("flux plot", fc.flux_plot),
                     ("rate csv", fc.rate_csv), ("rate plot", fc.rate_plot)):
        if p:
            lines.append(f"  {label}: {p}")
    return lines


def format_3d_cases(setups: dict) -> list[str]:
    """Render :func:`hydromate.threed.build_3d_cases` results as user-facing report
    lines (one block per steering file; a ``None`` setup reports why it was skipped).

    Kept here so every case's ``add3d.py`` prints the same summary; the builders
    live in :mod:`hydromate.threed` / :mod:`hydromate.unsteady`.
    """
    purposes = {
        "hydrostatic": ("steady boundary-flux convergence check (constant Q/H; the "
                        "sortie's FLUX BOUNDARY printouts show when in/outflow "
                        "balance)"),
        "hydrodyn": "steady non-hydrostatic flow at the in-file prescribed Q and H",
        "unsteady": ("hydrograph-driven: Q(t) inflow + outflow SL(t) via the same "
                     "liquid-boundaries file as unsteady2d.cas"),
    }
    lines: list[str] = []
    for key, s in setups.items():
        if lines:
            lines.append("")
        if s is None:
            lines.append(f"{key}: skipped - needs a varying boundaries.inflow "
                         "hydrograph (see the log message for details)")
            continue
        nh = getattr(s, "non_hydrostatic", True)
        lines.append(f"wrote {s.cas.name}:")
        lines.append(f"  pressure    : {'non-hydrostatic' if nh else 'hydrostatic'}")
        if hasattr(s, "liquid_boundaries"):        # the unsteady setup
            lines.append(f"  hydrograph  : {s.liquid_boundaries.name} "
                         f"over {s.duration:.0f} s")
        if hasattr(s, "dx"):
            lines.append(f"  vertical    : {s.n_levels} sigma levels "
                         f"(dz~{s.dz:.2f} m, dx~{s.dx:.2f} m, depth~{s.depth:.2f} m)")
        else:
            lines.append(f"  vertical    : {s.n_levels} sigma levels (dz~{s.dz:.2f} m)")
        if hasattr(s, "h_turbulence"):
            lines.append(f"  turbulence  : H={s.h_turbulence} V={s.v_turbulence} "
                         f"({s.turbulence_reason})")
        lines.append(f"  time step   : {s.time_step} s ({s.n_time_steps} steps ~ "
                     f"{s.time_step * s.n_time_steps:,.0f} s simulated)")
        gaia = getattr(s, "gaia_cas", None)
        if gaia:
            lines.append(f"  GAIA        : {gaia.name}")
        if key in purposes:
            lines.append(f"  purpose     : {purposes[key]}")
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
                         cwd: Path | str | None = None,
                         duration: float | None = None,
                         show_progress: bool = True,
                         sink: object | None = None,
                         should_stop=None):
    """Run the TELEMAC solver, echoing its listing live with a progress bar.

    Unlike a plain :meth:`TelemacRuntime.run_solver`, this streams the solver's
    stdout/stderr to the console as the run marches (so it is not silent) and, when
    *show_progress*, overlays a simulated-time progress bar parsed from TELEMAC's
    listing header. The bar's end time is *duration* when given (pass it whenever
    the ``.cas`` does not march to the config's own time cap - e.g. a per-layer 3D
    run or a hydrograph-spanning unsteady run), else :func:`expected_duration`.
    *cwd* overrides the run folder (default ``cfg.model_dir`` - the convergence
    studies run each level in its own subfolder). Returns the ``CompletedProcess``
    (its ``returncode`` reports success/failure; a non-zero exit does not raise).

    *sink* is an event sink (:mod:`hydromate.jobs.events`): the same parsed iteration
    and simulated time that drive the terminal bar are also emitted as structured
    progress, so a detached job never asks anyone to parse a console log. *should_stop*
    is polled between lines and terminates the whole solver process group when it
    returns true.
    """
    cas_file = cas_file or cfg.cas_file
    if ncsize is None:
        ncsize = cfg.telemac.n_processors
    if duration is None:
        duration = expected_duration(cfg)
    # A sink alone is enough reason to build the parser: a detached run wants the
    # structured progress even though it has no terminal to draw a bar on.
    progress = (SolverProgress(duration, sink=sink, echo=show_progress)
                if (show_progress or sink is not None) else None)
    on_line = progress.feed if progress else print
    try:
        return runtime.run_solver(cas_file, cwd=cwd or cfg.model_dir, ncsize=ncsize,
                                  solver=solver, check=False, on_line=on_line,
                                  should_stop=should_stop)
    finally:
        if progress is not None:
            progress.close()


# --------------------------------------------------------------------------- #
# post-run reporting (shared by every case's initial_run.py)
# --------------------------------------------------------------------------- #
def mesh_from_geometry(cfg: Config):
    """Rebuild a :class:`~hydromate.mesh.Mesh` from the built ``geometry.slf``.

    Enough of one for the geometry-only helpers (water table, node masks, areas):
    coordinates, element table, bed and the boundary numbering. Saves re-meshing
    just to ask a question about a finished run.
    """
    import numpy as np

    from hydromate.core import selafin
    from hydromate.mesh import Mesh

    geo = selafin.read_slf(cfg.model_path(cfg.geometry_slf))
    ipobo = np.asarray(geo["ipobo"])
    return Mesh(
        x=geo["x"], y=geo["y"], triangles=geo["ikle"],
        bottom=np.asarray(geo["values"]["BOTTOM"], dtype=float),
        ipobo=ipobo, boundary_nodes=np.flatnonzero(ipobo > 0),
        element_matid=np.ones(len(geo["ikle"]), dtype=int),
        node_matid=np.ones(len(geo["x"]), dtype=int),
    )


def water_table_mask(cfg: Config):
    """Depth the porous body's water table holds in place, per node (else ``None``).

    Rebuilt from the same config and the same seeded surface the build used, so the
    report and the model agree on which water is groundwater-fed rather than stray.
    Returns ``None`` - never raises - when the case has no water table or it cannot
    be reconstructed: this only decorates a report.
    """
    if cfg.gain_lose.water_table != "phreatic":
        return None
    try:
        import numpy as np

        from hydromate import watertable
        from hydromate.core import selafin

        mesh = mesh_from_geometry(cfg)
        ic = selafin.read_slf(cfg.model_path(cfg.ic_slf))["values"]["WATER DEPTH"]
        plane = watertable.fit_phreatic_plane(
            cfg, mesh, surface=np.asarray(mesh.bottom, dtype=float) + ic)
        if plane is None:
            return None
        return watertable.water_table_depth(
            plane, mesh, watertable.patch_node_mask(cfg, mesh))
    except Exception as exc:  # noqa: BLE001 - a reporting aid, never fatal
        log.debug("water-table mask unavailable (%s: %s)", type(exc).__name__, exc)
        return None


def report_wetting(cfg: Config) -> list[str]:
    """Wetted-extent + outlet-profile report for a finished run.

    A balanced flux budget says nothing about **where** the water is, which is the
    question this answers: how much of the wetted area actually carries flow, how
    much is stagnant film, how much the initial condition put there, and whether the
    film is still draining or has plateaued (a 2D model has neither infiltration nor
    evaporation, so water perched above the converged surface never leaves). The
    outlet profile then says whether the prescribed downstream stage is holding water
    up over ground that should be dry.

    Writes ``wetting-report.csv`` and ``outlet-profile.csv`` into ``model_dir`` and
    returns report lines. Never raises: a diagnostic must not fail a successful run.
    """
    from hydromate.wetting import outlet_profile, wetting_report

    results = cfg.model_path(cfg.results_slf)
    geometry = cfg.model_path(cfg.geometry_slf)
    lines: list[str] = []
    try:
        rep = wetting_report(
            results, geometry=geometry,
            initial_conditions=cfg.model_path(cfg.ic_slf),
            # water an external source holds in place (a water-table pool) is
            # legitimately wet; without this it counts as BOTH film and an isolated
            # puddle, i.e. reads as a defect when it is what the model intends
            supported=water_table_mask(cfg),
            wet_depth=cfg.hydrodynamics.wet_depth,
            out=cfg.model_dir,
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"wetting report skipped: {type(exc).__name__}: {exc}")
    else:
        lines.append(f"wetted-extent report -> {cfg.model_path('wetting-report.csv')}")
        lines += [f"  {ln}" for ln in rep.summary()]

    try:
        prof = outlet_profile(cfg, results, geometry=geometry, out=cfg.model_dir)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"outlet profile skipped: {type(exc).__name__}: {exc}")
    else:
        lines.append(f"outlet profile -> {cfg.model_path('outlet-profile.csv')}")
        lines += [f"  {ln}" for ln in prof.summary()]
    return lines


def report_sections(cfg: Config, out_name: str = "baffle-XS-q.csv") -> list[str]:
    """Discharge across the ``geodata.control_sections`` lines of a finished run.

    Integrates ``Q = int (H*U) . n ds`` from the result, so the sections can be drawn
    and changed in GIS without re-running the solver - which is how the split of the
    total discharge between the threads of a braided reach gets read off and checked
    against field transects. Empty list when the case defines no control sections.
    """
    if cfg.geodata.control_sections is None:
        return []
    from hydromate.sections import write_line_discharges

    try:
        df = write_line_discharges(
            cfg.model_path(cfg.results_slf),
            cfg.geodata.control_sections,
            cfg.model_path(out_name),
            geometry=cfg.model_path(cfg.geometry_slf),
            name_field=cfg.geodata.control_section_name_field,
            crs_epsg=cfg.crs_epsg,
        )
    except Exception as exc:  # noqa: BLE001 - the run already succeeded
        return [f"cross-section discharges skipped: {type(exc).__name__}: {exc}"]
    lines = [f"cross-section discharges -> {cfg.model_path(out_name)}"]
    for _, r in df.iterrows():
        lines.append(
            f"  {r['name']:<16} {r['discharge']:8.4f} m3/s   "
            f"(wet {r['wetted_width']:5.1f} m, mean h {r['mean_depth']:.3f} m,"
            f" mean |U| {r['mean_velocity']:.3f} m/s)")
    return lines
