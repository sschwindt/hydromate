"""Stage 4 - steering files: friction ``.tbl`` and the TELEMAC ``.cas`` (+ GAIA stub).

The friction table lists one row per MATID zone (``<id> <LAW> <coef> NULL``,
terminated by ``END``); node-to-zone linkage is carried by the ``FRIC_ID``
variable in the geometry SELAFIN (written in stage 2). HydroBayesCal perturbs the
coefficient column of this table during calibration.

The ``.cas`` is assembled from a small set of building blocks plus user overrides
so the case is reproducible and the prescribed boundary values stay consistent
with the liquid-boundary numbering from stage 3.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from hydromate.config import Config
from hydromate.boundary import LiquidBoundary

log = logging.getLogger("hydromate")

# friction-law number -> 4-letter name used in the .tbl
LAW_NAMES = {0: "NOFR", 1: "HAAL", 2: "CHEZ", 3: "STRI", 4: "MANN", 5: "NIKU", 7: "COWH"}

# TELEMAC-2D TURBULENCE MODEL number -> name
TURB_NAMES = {1: "constant viscosity", 2: "Elder", 3: "k-epsilon",
              4: "Smagorinski", 6: "Spalart-Allmaras"}
# accepted names for an explicit hydrodynamics.turbulence_model
TURB_ALIASES = {
    "constant": 1, "constant-viscosity": 1, "const": 1, "elder": 2,
    "k-epsilon": 3, "k_epsilon": 3, "kepsilon": 3, "k-e": 3, "ke": 3,
    "smagorinski": 4, "smagorinsky": 4, "les": 4,
    "spalart-allmaras": 6, "spalart_allmaras": 6, "spalart": 6, "sa": 6,
}

# 80% of the TKE is resolved (so LES / Smagorinski is justified) once the cell /
# filter width dx is small enough that the unresolved energy above the spectral
# cutoff - which in the inertial subrange (E(k) ~ k^-5/3) scales as (dx/L)^(2/3) -
# drops below 20%:  (dx/L)^(2/3) <= 0.2  <=>  dx/L <= 0.2**1.5 (~0.089, i.e. >= ~11
# cells per integral length scale L). Then the sub-grid model handles <20% of the
# dissipation, the LES validity requirement.
LES_TKE_FRACTION = 0.80
_LES_RATIO = (1.0 - LES_TKE_FRACTION) ** 1.5     # dx/L threshold for the LES gate
_KEPS_MIN_CELLS = 4.0                            # cells per L for the k-epsilon range


def _coerce_turbulence_model(value) -> int | None:
    """Resolve an explicit turbulence setting to a TELEMAC model number, or None
    when it requests auto-selection ("auto"/empty)."""
    if value is None:
        return None
    if isinstance(value, str):
        key = value.strip().lower()
        if key in ("", "auto"):
            return None
        if key in TURB_ALIASES:
            return TURB_ALIASES[key]
        return int(key)                          # a numeric string like "3"
    return int(value)


def _turbulence_length_scale(cfg: Config) -> float:
    """Turbulence integral length scale [m] - the flow depth proxy: the explicit
    override, else the pre-wet depth, else 1 m."""
    h = cfg.hydrodynamics
    if h.turbulence_length_scale is not None:
        return float(h.turbulence_length_scale)
    if cfg.initialization.prewet_depth is not None:
        return float(cfg.initialization.prewet_depth)
    return 1.0


def eddy_viscosity_estimate(cfg: Config) -> float:
    """Depth-averaged turbulent eddy viscosity [m2/s] from the velocity guess.

    Elder-type closure ``nu_t = alpha * u_star * h`` with the shear velocity
    ``u_star ~ 0.1 * U`` (a skin-friction proxy C_f ~ 0.01) and the transverse-mixing
    coefficient ``alpha = 0.6``; ``h`` is the turbulence length scale (flow depth).
    Used as the constant VELOCITY DIFFUSIVITY (model 1) and reported for context."""
    u = abs(float(cfg.hydrodynamics.initial_velocity_guess))
    h = _turbulence_length_scale(cfg)
    return max(0.6 * (0.1 * u) * h, 1e-6)


def _channel_cell_size(cfg: Config, mesh) -> float:
    """Representative channel cell edge length [m] (median edge of channel-side
    cells). Without a mesh, falls back to the effective configured size
    (:func:`hydromate.mesh.nominal_channel_size` - gpkg per-zone sizes and
    ``mesh.size_scale`` included, so a scaled convergence level is seen)."""
    import numpy as np

    from hydromate import mesh as mesh_mod

    if mesh is None:
        return mesh_mod.nominal_channel_size(cfg)

    tri = mesh.triangles
    try:                                         # restrict to channel cells if zoned
        in_channel = mesh_mod.channel_node_mask(cfg, mesh)
        sel = in_channel[tri].sum(axis=1) >= 2
        if sel.any():
            tri = tri[sel]
    except Exception:                            # no channel zones -> whole mesh
        pass
    p = np.column_stack([mesh.x, mesh.y])
    edges = np.concatenate([
        np.linalg.norm(p[tri[:, 0]] - p[tri[:, 1]], axis=1),
        np.linalg.norm(p[tri[:, 1]] - p[tri[:, 2]], axis=1),
        np.linalg.norm(p[tri[:, 2]] - p[tri[:, 0]], axis=1),
    ])
    return float(np.median(edges)) if edges.size else mesh_mod.nominal_channel_size(cfg)


def select_turbulence_model(cfg: Config, mesh=None) -> tuple[int, str]:
    """Pick the TELEMAC turbulence model from the mesh resolution and velocity guess.

    An explicit ``hydrodynamics.turbulence_model`` (int or name) is honoured as-is.
    With ``"auto"`` the choice follows the channel cell size ``dx`` relative to the
    turbulence length scale ``L`` (the flow depth) - a mesh-size/velocity criterion:

    * **Smagorinski LES (4)** when the mesh resolves >=80% of the TKE
      (``dx/L <= 0.2**1.5``, ~11+ cells per ``L``): the sub-grid model then handles
      <20% of the dissipation, the LES validity requirement.
    * **k-epsilon (3)** at moderate resolution (>= ~4 cells per ``L`` but below the
      LES gate): a full two-equation RANS closure resolving the mean shear.
    * **Spalart-Allmaras (6)** on coarse meshes (< ~4 cells per ``L``): a robust,
      economical one-equation RANS model, tolerant of the stretched channel cells.

    The velocity guess sets the eddy-viscosity scale (and the steady initial flow
    field); for a fixed ``L`` it cancels from the resolution ratio, so the cell size
    drives the choice. Returns ``(model_number, rationale)``.
    """
    explicit = _coerce_turbulence_model(cfg.hydrodynamics.turbulence_model)
    if explicit is not None:
        return explicit, f"configured explicitly: {TURB_NAMES.get(explicit, explicit)}"
    return turbulence_pick_for_dx(cfg, _channel_cell_size(cfg, mesh))


def turbulence_pick_for_dx(cfg: Config, dx: float) -> tuple[int, str]:
    """The mesh-resolution auto-selection of :func:`select_turbulence_model` for an
    arbitrary channel cell size *dx* [m], ignoring any explicit
    ``hydrodynamics.turbulence_model`` override.

    Used by the mesh-convergence validity check to ask "what *would* auto-selection
    pick at this refinement?" while the study runs with the closure pinned to the
    baseline choice. Returns ``(model_number, rationale)``.
    """
    import numpy as np

    u = float(cfg.hydrodynamics.initial_velocity_guess)
    length = _turbulence_length_scale(cfg)
    ratio = dx / length if length > 0 else np.inf
    cells = length / dx if dx > 0 else np.inf
    resolved = float(np.clip(1.0 - ratio ** (2.0 / 3.0), 0.0, 1.0)) if np.isfinite(ratio) else 0.0
    nu_t = eddy_viscosity_estimate(cfg)
    tail = (f"channel cell {dx:.2f} m, L~{length:.2f} m ({cells:.1f} cells/L, "
            f"~{resolved * 100:.0f}% TKE resolved), U~{u:.1f} m/s, nu_t~{nu_t:.3f} m2/s")
    if ratio <= _LES_RATIO:
        return 4, f"Smagorinski LES: >=80% TKE resolved, sub-grid <20% dissipation; {tail}"
    if cells >= _KEPS_MIN_CELLS:
        return 3, f"k-epsilon: moderate resolution, two-equation RANS; {tail}"
    return 6, f"Spalart-Allmaras: coarse mesh, robust one-equation RANS; {tail}"


def _friction_rows(cfg: Config) -> list[tuple[int, int, float, str]]:
    """Return the friction zones as (fric_id, law, coefficient, name) rows.

    Priority: explicit ``friction.zones`` (MATID scheme) > the roughness zones
    (``geodata.roughness_zones`` + ``roughness_table``: one row per Zone ID with
    its ks under ``friction.roughness_law``, default NIKU) > a single default zone.
    """
    if cfg.friction.zones:
        return [(z.matid, z.law, z.coefficient, z.name)
                for z in sorted(cfg.friction.zones, key=lambda z: z.matid)]
    if cfg.geodata.roughness_zones is not None and cfg.geodata.roughness_table is not None:
        from hydromate.mesh import read_roughness_table

        table = read_roughness_table(cfg.geodata.roughness_table)
        return [(zid, cfg.friction.roughness_law, ks, f"roughness zone {zid}")
                for zid, ks in sorted(table.items())]
    return [(1, cfg.friction.default_law, cfg.friction.default_coefficient, "default")]


def _global_friction(cfg: Config) -> tuple[int, float]:
    """Effective (LAW OF BOTTOM FRICTION, FRICTION COEFFICIENT) for the .cas.

    With a FRICTION DATA FILE the per-zone laws/coefficients come from the .tbl;
    these globals just need to be consistent, so reuse the first .tbl row."""
    rows = _friction_rows(cfg)
    return rows[0][1], rows[0][2]


def write_friction_tbl(cfg: Config) -> Path:
    """Write the FRICTION DATA FILE (.tbl), one row per friction zone.

    Zones come from ``friction.zones`` (MATID) or, failing that, from the
    roughness zones (Zone ID -> ks via ``roughness_table``) so the rows match the
    geometry's per-node ``FRIC_ID``."""
    lines = [
        "* Friction data file generated by hydromate",
        "* Bed roughness laws: NOFR | HAAL | CHEZ | STRI | MANN | NIKU | COWH",
        "* Columns: Fric_ID  BottomLaw  Coefficient  Mdef",
        "*",
    ]
    for fric_id, law_num, coef, name in _friction_rows(cfg):
        law = LAW_NAMES.get(law_num, "MANN")
        # data rows carry only numeric columns; names go in a '*' comment
        lines.append(f"* zone {fric_id}: {name}")
        lines.append(f"{fric_id}\t{law}\t{coef:.4f}\tNULL")
    lines.append("END")
    path = cfg.model_path(cfg.friction_tbl)
    path.write_text("\n".join(lines) + "\n")
    return path


def _centerline_arclength(cfg: Config, mesh):
    """Per-node arc length: the along-reach distance of the nearest channel-
    centerline vertex, used to order channel nodes from upstream to downstream."""
    import geopandas as gpd
    import numpy as np
    from scipy.spatial import cKDTree
    from shapely.ops import linemerge, unary_union

    gdf = gpd.read_file(cfg.geodata.channel_centerline)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    merged = unary_union(gdf.geometry.values)
    line = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
    if line.geom_type == "MultiLineString":          # disjoint parts: take longest
        line = max(line.geoms, key=lambda g: g.length)
    n = max(2, int(line.length / 2.0))
    sline = np.linspace(0.0, line.length, n)
    cpts = np.array([[line.interpolate(d).x, line.interpolate(d).y] for d in sline])
    _, idx = cKDTree(cpts).query(np.column_stack([mesh.x, mesh.y]))
    return sline[idx]


def _longitudinal_prewet_depth(s_node, bottom, mask, depth_val, bed_percentile=25.0):
    """Pre-wet water depth from a *longitudinally smoothed thalweg* profile.

    Channel nodes are binned along the reach (arc length *s_node*); a low percentile
    of the bed per bin (the thalweg, robust to pits) is smoothed and interpolated
    into a target water surface ``wl(s) = thalweg(s) + depth_val``. The depth is
    ``max(wl - bottom, 0)`` on channel nodes (dry elsewhere): laterally near-flat per
    cross-section, following the reach gradient, ~``depth_val`` over the low-flow
    channel and tapering to 0 up the banks. Referencing the thalweg (not the section
    mean, which the high banks inflate) keeps the seeded surface close to the steady
    profile, so the hotstart drains only a little before converging - while still
    removing the jagged surface / channel-edge "dam" of a constant-depth seed that
    diverges on steep bathymetry.
    """
    import numpy as np

    s_ch, bed_ch = s_node[mask], bottom[mask]
    span = float(s_ch.max() - s_ch.min()) or 1.0
    nbins = int(np.clip(span / 5.0, 8, 400))             # ~5 m longitudinal bins
    edges = np.linspace(s_ch.min(), s_ch.max() + 1e-9, nbins + 1)
    which = np.clip(np.digitize(s_ch, edges) - 1, 0, nbins - 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    binbed = np.full(nbins, np.nan)
    for b in range(nbins):                               # per-bin thalweg (percentile)
        sel = bed_ch[which == b]
        if sel.size:
            binbed[b] = np.percentile(sel, bed_percentile)
    valid = ~np.isnan(binbed)
    binbed = np.interp(centers, centers[valid], binbed[valid])
    # moving-average smoothing along the reach (~1/25 of its length)
    k = max(1, nbins // 25)
    binbed = np.convolve(np.pad(binbed, k, mode="edge"),
                         np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    wl = np.interp(s_node, centers, binbed) + depth_val
    return np.where(mask, np.maximum(wl - bottom, 0.0), 0.0)


def write_initial_conditions(cfg: Config, mesh) -> Path:
    """Write the hotstart SELAFIN that pre-wets the channel for a stable start.

    Seeds WATER DEPTH on the ``*channel*`` mesh-zone nodes (zero velocity, dry
    elsewhere) on the geometry's own mesh; TELEMAC reads it as the PREVIOUS
    COMPUTATION FILE (see :func:`write_cas`) so the run continues from a pre-wetted
    channel instead of advancing the wetting front from a dry bed.

    With a channel centerline the seeded water surface is a *longitudinally smoothed*
    surface following the reach gradient (:func:`_longitudinal_prewet_depth`) - a
    constant depth instead makes the surface as jagged as the bed and leaves a depth
    "dam" at every channel/floodplain edge, which diverges at t=0 on steep terrain.
    Without a centerline it falls back to the simple constant-depth seed.
    """
    import numpy as np

    from hydromate import mesh as mesh_mod
    from hydromate import selafin

    depth_val = float(cfg.initialization.prewet_depth)
    mask = mesh_mod.channel_node_mask(cfg, mesh)
    if cfg.geodata.channel_centerline is not None:
        s_node = _centerline_arclength(cfg, mesh)
        depth = _longitudinal_prewet_depth(s_node, mesh.bottom, mask, depth_val)
        how = "smoothed longitudinal surface"
    else:
        depth = np.where(mask, depth_val, 0.0)
        how = "constant depth (no centerline)"
    path = cfg.model_path(cfg.ic_slf)
    selafin.write_initial_state(
        path, x=mesh.x, y=mesh.y, ikle=mesh.triangles + 1, ipobo=mesh.ipobo,
        depth=depth, title=f"{cfg.name} initial conditions",
    )
    log.info("  pre-wet %d/%d channel nodes (%s; ~%.2f m over thalweg, max %.2f m) -> %s",
             int(mask.sum()), mask.size, how, depth_val, float(depth.max()), path.name)
    return path


def write_dry_start_conditions(cfg: Config, mesh) -> Path | None:
    """Write the DRY-START initial-conditions SELAFIN: a thin water plug only on the
    nodes near the inflow line(s), dry everywhere else.

    A fully dry bed (``ZERO DEPTH``) makes TELEMAC's DEBIMP abort at a prescribed-Q
    inflow ("PROBLEM ON BOUNDARY ... CHECK THE WATER DEPTHS"): with no water at the
    boundary the discharge cannot be distributed. So instead of pre-wetting the whole
    channel, we wet *only* a strip within ``dry_start_extent`` of the inflow line(s)
    to ``dry_start_depth`` (deep enough to keep the inflow subcritical) and leave the
    rest of the domain dry. TELEMAC continues from this file and the flow wets the
    reach from the inflow. Returns the path, or ``None`` when there is no inflow line
    (the caller then uses the analytical dry initial condition).
    """
    import numpy as np
    from shapely import contains_xy

    from hydromate import boundary, selafin

    inflow = boundary._load_liquid_lines(cfg).get("inflow")
    if inflow is None:
        return None
    init = cfg.initialization
    extent = (float(init.dry_start_extent) if init.dry_start_extent is not None
              else max(cfg.mesh.channel_size, cfg.mesh.floodplain_size)
              * cfg.mesh.size_scale * 5.0)
    seed = float(init.dry_start_depth)
    plug = np.asarray(contains_xy(inflow.buffer(extent), mesh.x, mesh.y), dtype=bool)
    depth = np.where(plug, seed, 0.0)
    path = cfg.model_path(cfg.ic_slf)
    selafin.write_initial_state(
        path, x=mesh.x, y=mesh.y, ikle=mesh.triangles + 1, ipobo=mesh.ipobo,
        depth=depth, title=f"{cfg.name} dry start (inflow plug)",
    )
    log.info("  dry start: seeded %d/%d inflow-plug nodes to %.2f m (within %.1f m of "
             "the inflow), dry elsewhere -> %s",
             int(plug.sum()), plug.size, seed, extent, path.name)
    return path


def _prescribed_arrays(cfg: Config, liquids: list[LiquidBoundary],
                       inflow_q: float, outflow_wse: float | None) -> tuple[str, str, str]:
    """Build PRESCRIBED FLOWRATES / ELEVATIONS / VELOCITY PROFILES, ordered by
    liquid-boundary index. Unused slots get a harmless placeholder.

    A free (Neumann) outflow prescribes nothing, so its elevation slot is a
    placeholder; ``stage_discharge`` / ``elevation`` instead prescribe *outflow_wse*.

    *inflow_q* is the **total** reach discharge. When several inflow boundaries
    exist (e.g. an upstream cross-section split by an island), it is distributed
    across them in proportion to their node count (a width/conveyance proxy) so the
    total prescribed inflow stays *inflow_q* - prescribing the full Q on each would
    multiply the supplied discharge and flood the domain.
    """
    free_outflow = cfg.boundaries.outflow_condition == "free"
    inflows = [b for b in liquids if b.kind == "inflow"]
    inflow_nodes = sum(b.n_nodes for b in inflows) or 1
    flow, elev, prof = [], [], []
    for lb in sorted(liquids, key=lambda b: b.index):
        if lb.kind == "inflow":
            share = inflow_q * lb.n_nodes / inflow_nodes
            flow.append(f"{share:.4f}")
            elev.append("0.")                   # ignored for an inflow boundary
            prof.append("4")                    # ~ proportional to sqrt(depth)
        else:
            flow.append("0.")                   # ignored for an outflow boundary
            elev.append("0." if free_outflow else f"{outflow_wse:.4f}")
            prof.append("1")                    # constant normal profile
    return ";".join(flow), ";".join(elev), ";".join(prof)


def write_liquid_boundaries(cfg: Config, liquids: list[LiquidBoundary],
                            inflow, outflow_wse_fn=None) -> Path:
    """Write the TELEMAC liquid-boundaries (hydrograph) file for the unsteady run.

    One column per liquid boundary, in boundary-index order: ``Q(i)`` for inflows
    (the total reach discharge at each time split across the inflow boundaries by
    node share, exactly as the steady prescribed flowrates) and ``SL(i)`` for
    prescribed-elevation outflows (``outflow_wse_fn(Q)`` - the rating-curve stage at
    that discharge). A free (Neumann) outflow prescribes nothing and gets no column.
    The first column is the time ``T`` in seconds.
    """
    import numpy as np

    free_outflow = cfg.boundaries.outflow_condition == "free"
    ordered = sorted(liquids, key=lambda b: b.index)
    inflows = [b for b in ordered if b.kind == "inflow"]
    inflow_nodes = sum(b.n_nodes for b in inflows) or 1
    times = inflow.times_s
    q = np.asarray(inflow.discharge, dtype=float)
    if times is None or len(times) < 2:          # degenerate: hold a constant value
        times = np.array([0.0, 3600.0])
        q = np.array([q[-1], q[-1]])

    header = ["T"] + [f"Q({lb.index})" if lb.kind == "inflow" else f"SL({lb.index})"
                      for lb in ordered if lb.kind == "inflow" or not free_outflow]
    units = ["s"] + ["m3/s" if c.startswith("Q") else "m" for c in header[1:]]
    lines = ["# liquid-boundaries hydrograph generated by hydromate", " ".join(header),
             " ".join(units)]
    for t, qt in zip(np.asarray(times, dtype=float), q):
        row = [f"{t:.1f}"]
        for lb in ordered:
            if lb.kind == "inflow":
                row.append(f"{qt * lb.n_nodes / inflow_nodes:.4f}")
            elif not free_outflow:
                row.append(f"{(outflow_wse_fn(qt) if outflow_wse_fn else 0.0):.4f}")
        lines.append(" ".join(row))
    path = cfg.model_path(cfg.liquid_boundaries_file)
    path.write_text("\n".join(lines) + "\n")
    return path


def write_cas(cfg: Config, liquids: list[LiquidBoundary],
              inflow_q: float, outflow_wse: float | None = None,
              gaia_cas: str | None = None,
              previous_computation: str | None = None,
              turbulence_model: int | None = None,
              unsteady: bool = False, liquid_boundaries_file: str | None = None,
              duration: float | None = None, out_name: str | None = None,
              results_name: str | None = None,
              n_distributive_corrections: int | None = None,
              sections_input: str | None = None, sections_output: str | None = None,
              hotstart_note: str | None = None) -> Path:
    """Write a TELEMAC-2D steering (.cas) file.

    The default (``unsteady=False``, ``out_name=None``) writes the **steady** initial
    run to ``cfg.cas_file`` (``steady2d.cas``). With ``unsteady=True`` and a
    ``liquid_boundaries_file`` it writes a hydrograph-driven run (Q(t)/SL(t) read
    from that file, total time from ``duration``) - the pipeline emits it as
    ``unsteady2d.cas`` when the inflow carries a varying series, and
    :mod:`hydromate.unsteady` writes the recommended one hotstarted from the steady
    result.

    *turbulence_model* is the resolved TELEMAC model number (see
    :func:`select_turbulence_model`); when None it is selected from the config here.

    When *previous_computation* is given the case is continued from that SELAFIN -
    the flow field is read from it instead of the analytical INITIAL CONDITIONS, with
    the clock reset to zero. This is either the pre-wetted/dry-start hotstart from
    :func:`write_initial_conditions` (the steady run) or the converged **steady result**
    the unsteady run continues from; *hotstart_note* overrides the comment line.

    *results_name* overrides the RESULTS FILE (so an unsteady run continued from the
    steady ``r2d.slf`` writes to a distinct file rather than clobbering its own input).
    *n_distributive_corrections* emits ``NUMBER OF CORRECTIONS OF DISTRIBUTIVE
    SCHEMES`` (the developers' >=2 recommendation for quasi-steady runs).
    *sections_input* / *sections_output* wire the CONTROL SECTIONS keywords so the run
    reports the flux across each open boundary (see :func:`hydromate.unsteady`).
    """
    h = cfg.hydrodynamics
    flow, elev, prof = _prescribed_arrays(cfg, liquids, inflow_q, outflow_wse)
    n_liquid = len(liquids)
    regime = "unsteady" if unsteady else "steady"
    if turbulence_model is None:
        turbulence_model = select_turbulence_model(cfg, None)[0]
    # finalise the turbulence model up front (finite volumes accept only model 1) so
    # the graphic-printout list and every block below agree on the same model.
    turb_model = turbulence_model
    if h.finite_volumes and turb_model != 1:
        # FV's init_fv.f rejects ITURB>=2 ('TURBULENCE MODEL NOT TAKEN INTO ACCOUNT');
        # k-epsilon (3) / Smagorinski (4) / Spalart-Allmaras (6) need finite elements.
        log.warning("finite volumes accept only TURBULENCE MODEL 1 (constant "
                    "viscosity); overriding the selected model %d (%s)",
                    turb_model, TURB_NAMES.get(turb_model, "?"))
        turb_model = 1
    # K (TKE) and E (its dissipation) only exist for the k-epsilon model (3); request
    # them as graphic outputs there so the results carry the turbulence fields too.
    printvars = "U,V,S,B,H,M,Q,F" + (",K,E" if turb_model == 3 else "")

    if previous_computation:
        # since TELEMAC release 9.0 the boolean COMPUTATION CONTINUED keyword is
        # gone; supplying PREVIOUS COMPUTATION FILE alone triggers the continuation.
        initial_conditions = [
            hotstart_note or "/ pre-wetted hotstart (channel seeded with water; see hydromate)",
            f"PREVIOUS COMPUTATION FILE : {previous_computation}",
            "INITIAL TIME SET TO ZERO : YES",
        ]
    else:
        initial_conditions = [f"INITIAL CONDITIONS : '{cfg.initialization.initial_conditions}'"]

    # how the run is bounded. A hydrograph run uses DURATION (seconds). For the steady
    # march, NUMBER OF TIME STEPS bounds a FIXED-step run - but it does NOT bound a
    # VARIABLE TIME-STEP run (the CFL-driven dt is unknown a priori, so TELEMAC needs a
    # simulated DURATION; with NUMBER OF TIME STEPS the run never terminates). So cap a
    # variable-step run by DURATION = n_time_steps * time_step; the steady-state
    # auto-stop ends it at equilibrium well before this generous fallback.
    if duration is not None:
        duration_line = f"DURATION : {duration:.1f}"
    elif h.variable_timestep:
        # explicit hydrodynamics.duration decouples the simulated time from the small
        # CFL start step; else fall back to n_time_steps * time_step.
        sim_duration = h.duration if h.duration is not None else h.n_time_steps * h.time_step
        duration_line = f"DURATION : {sim_duration:.1f}"
    else:
        duration_line = f"NUMBER OF TIME STEPS : {h.n_time_steps}"

    lines: list[str] = [
        "/" + "-" * 68,
        f"/ TELEMAC2D steering file generated by hydromate for case '{cfg.name}'",
        f"/ regime: {regime}",
        "/" + "-" * 68,
        f"TITLE : '{cfg.name} {regime}'",
        "/",
        "/ INPUT / OUTPUT FILES",
        f"GEOMETRY FILE : {cfg.geometry_slf}",
        f"BOUNDARY CONDITIONS FILE : {cfg.boundary_cli}",
        *([f"LIQUID BOUNDARIES FILE : {liquid_boundaries_file}"]
          if (unsteady and liquid_boundaries_file) else []),
        f"RESULTS FILE : {results_name or cfg.results_slf}",
        # control sections: report the flux across each open boundary (verifies
        # Q_in(t) vs Q_out(t) for the unsteady run) - see hydromate.unsteady.
        *([f"SECTIONS INPUT FILE : {sections_input}",
           f"SECTIONS OUTPUT FILE : {sections_output}"]
          if (sections_input and sections_output) else []),
        "MASS-BALANCE : YES",
        # M = scalar velocity (sqrt(u^2+v^2)); written so the results carry
        # SCALAR VELOCITY directly, which HydroBayesCal reads as a calibration QoI.
        # K,E (TKE + dissipation) are appended for the k-epsilon model (see above).
        f"VARIABLES FOR GRAPHIC PRINTOUTS : '{printvars}'",
        "PRINTING CUMULATED FLOWRATES : YES",
        "/",
        "/ TIME",
        f"TIME STEP : {h.time_step}",
        # CFL-adaptive marching: with VARIABLE TIME-STEP the TIME STEP above is just
        # the initial/maximum, and TELEMAC shrinks it to hold DESIRED COURANT NUMBER
        # (essential on the fine channel mesh, where a fixed 1 s step is unstable).
        *(["VARIABLE TIME-STEP : YES",
           f"DESIRED COURANT NUMBER : {h.desired_courant}"]
          if h.variable_timestep else []),
        duration_line,
        f"GRAPHIC PRINTOUT PERIOD : {h.graphic_printout_period}",
        f"LISTING PRINTOUT PERIOD : {h.listing_printout_period}",
        # OPT-IN steady-state auto-stop (off by default, steady run only). TELEMAC's
        # steady.f stops when |X - X_prev| over two CONSECUTIVE TIME STEPS falls below
        # STOP CRITERIA - an ABSOLUTE per-step change. With VARIABLE TIME-STEP the dt is
        # tiny, so this triggers during a slow transient (still-filling reach) long
        # before the fluxes balance; convergence is judged by the flux balance instead
        # (hydromate.flux_convergence). Only emit it for a steady, FIXED-step run.
        *(["STOP IF A STEADY STATE IS REACHED : YES",
           f"STOP CRITERIA : {h.stop_criteria}"]
          if (h.stop_if_steady and not unsteady and not h.variable_timestep) else []),
        "/",
    ]

    # numerics: finite elements (default) vs finite volumes ------------------
    if h.finite_volumes:
        lines += [
            "/ NUMERICS (finite volumes: robust for transcritical wetting/drying;",
            "/ explicit scheme -> time step is CFL-bound via VARIABLE TIME-STEP above)",
            "EQUATIONS : 'SAINT-VENANT FV'",
            f"FINITE VOLUME SCHEME : {h.finite_volume_scheme}",        # 5 = HLLC
            f"FINITE VOLUME SCHEME SPACE ORDER : {h.fv_space_order}",  # 2 = MUSCL
        ]
    else:
        lines += [
            "/ NUMERICS (finite elements)",
            "ADVECTION : YES",
            "TREATMENT OF THE LINEAR SYSTEM : 2",
            # LINEAR elements for H and U,V - required by the distributive advection
            # scheme 14 (else TELEMAC stops on quasi-bubble/quadratic elements).
            f"DISCRETIZATIONS IN SPACE : {h.discretizations_in_space}",
            "SCHEME FOR ADVECTION OF VELOCITIES : 14",
            f"MAXIMUM NUMBER OF ITERATIONS FOR ADVECTION SCHEMES : {h.max_advection_iterations}",
            f"NUMBER OF SUB-ITERATIONS FOR NON-LINEARITIES : {h.advection_sub_iterations}",
            # predictor-corrector (distributive) schemes need >=2 corrections per step
            # for a well-converged QUASI-STEADY march (TELEMAC2d manual 7.2.1); the
            # unsteady run sets this, the steady run leaves it at the solver default.
            *([f"NUMBER OF CORRECTIONS OF DISTRIBUTIVE SCHEMES : {n_distributive_corrections}"]
              if n_distributive_corrections is not None else []),
            # more-implicit depth/velocity update (0.80 > the explicit 0.55 default)
            # keeps the wetting front from oscillating into divergence.
            f"IMPLICITATION FOR DEPTH : {h.implicitation:.2f}",
            f"IMPLICITATION FOR VELOCITY : {h.implicitation:.2f}",
            "MASS-LUMPING ON H : 1.",
            "MASS-LUMPING ON VELOCITY : 1.",
            # a preconditioned solver (2 + PRECONDITIONING 2) converges where the
            # plain conjugate gradient (1) stalls on the ill-conditioned fine,
            # high-aspect channel-mesh system ('GRACJG EXCEEDING MAXIMUM ITERATIONS')
            f"SOLVER : {h.solver}",
            f"PRECONDITIONING : {h.preconditioning}",
            f"SOLVER ACCURACY : {h.solver_accuracy}",
            "MAXIMUM NUMBER OF ITERATIONS FOR SOLVER : 200",
        ]

    # constant eddy viscosity for model 1 (incl. the forced FV case): honour an
    # explicit value, else estimate it from the velocity guess (Elder closure).
    diffusivity = (h.velocity_diffusivity if h.velocity_diffusivity is not None
                   else eddy_viscosity_estimate(cfg) if turb_model == 1 else None)

    lines += [
        "/",
        "/ STABILITY CONTROLS",
        # damps free-surface instabilities over steep bed gradients (default 1.0)
        f"FREE SURFACE GRADIENT COMPATIBILITY : {h.free_surface_gradient_compat}",
        # clip H/U/V/T so a local spike can't cascade to NaN; a divergence guard
        "CONTROL OF LIMITS : YES",
        "LIMIT VALUES : -1000;9000;-1000;1000;-1000;1000;-1000;1000",
    ]
    # tidal flats: FE needs the explicit wetting/drying treatment; FV handles dry
    # fronts intrinsically (the HLLC Riemann solver), so this block is FE-only.
    if not h.finite_volumes:
        lines += [
            "/",
            "/ TIDAL FLATS",
            "TIDAL FLATS : YES",
            "CONTINUITY CORRECTION : YES",
            "OPTION FOR THE TREATMENT OF TIDAL FLATS : 1",
            "TREATMENT OF NEGATIVE DEPTHS : 2",
            # leave H unclipped so drying is handled by the treatment above, not by a
            # hard depth clip that would inject mass and destabilise the wetting front.
            f"H CLIPPING : {'YES' if h.h_clipping else 'NO'}",
        ]

    lines += [
        "/",
        "/ BOUNDARY CONDITIONS",
        f"PRESCRIBED FLOWRATES : {flow}",
        f"PRESCRIBED ELEVATIONS : {elev}",
        f"VELOCITY PROFILES : {prof}",
        f"OPTION FOR LIQUID BOUNDARIES : {';'.join(['1'] * n_liquid)}",
        "/",
        "/ FRICTION (zonal; coefficients calibrated by HydroBayesCal)",
        f"LAW OF BOTTOM FRICTION : {_global_friction(cfg)[0]}",
        f"FRICTION COEFFICIENT : {_global_friction(cfg)[1]}",
        "FRICTION DATA : YES",
        f"FRICTION DATA FILE : {cfg.friction_tbl}",
        # lateral-boundary (wall) friction is a finite-element feature; the FV kernel
        # ignores it and then aborts ('WALL FRICTION NOT TAKEN INTO ACCOUNT'), so
        # emit it only for finite elements.
        *([f"LAW OF FRICTION ON LATERAL BOUNDARIES : {cfg.friction.boundary_law}",
           f"ROUGHNESS COEFFICIENT OF BOUNDARIES : {cfg.friction.boundary_coefficient}"]
          if not h.finite_volumes else []),
        "/",
        "/ INITIAL CONDITIONS",
        *initial_conditions,
        "/",
        f"/ TURBULENCE ({TURB_NAMES.get(turb_model, turb_model)})",
        "DIFFUSION OF VELOCITY : YES",
        # turbulence closure resolved by select_turbulence_model (or configured).
        # VELOCITY DIFFUSIVITY is the constant eddy viscosity for model 1 (the only
        # model finite volumes accept); for model 1 it defaults to the velocity-guess
        # estimate when not set explicitly.
        f"TURBULENCE MODEL : {turb_model}",
        # loosen the turbulence-transport solver accuracy from TELEMAC's 1e-9 default
        # (unreachable in the 50-iteration cap while the dry-start domain is still
        # ill-conditioned -> the transient "GRACJG: EXCEEDING MAXIMUM ITERATIONS 50"
        # warning); 1e-6 is plenty for k/epsilon and converges in fewer iterations.
        *([f"ACCURACY OF K : {h.turbulence_solver_accuracy}",
           f"ACCURACY OF EPSILON : {h.turbulence_solver_accuracy}",
           # raise the k-epsilon solve budget (default 50 is too few on the
           # ill-conditioned dry-start domain) without relaxing the accuracy above.
           f"MAXIMUM NUMBER OF ITERATIONS FOR K AND EPSILON : {h.max_keps_iterations}"]
          if turb_model == 3 else []),
        *([f"ACCURACY OF SPALART-ALLMARAS : {h.turbulence_solver_accuracy}"]
          if turb_model == 6 else []),
        *([f"VELOCITY DIFFUSIVITY : {diffusivity:g}"] if diffusivity is not None else []),
    ]

    if cfg.morphodynamics.enabled and gaia_cas:
        # COUPLING WITH : 'GAIA' internally couples the sediment solver; GAIA declares
        # its own tracers for suspended load, so no manual NUMBER OF TRACERS is needed.
        lines += ["/", "/ MORPHODYNAMICS (GAIA - bedload / suspended load)",
                  "COUPLING WITH : 'GAIA'",
                  f"GAIA STEERING FILE : {gaia_cas}",
                  f"COUPLING PERIOD FOR GAIA : {cfg.morphodynamics.coupling_period}"]

    if h.extra_keywords:
        lines += ["/", "/ USER OVERRIDES"]
        for key, value in h.extra_keywords.items():
            lines.append(f"{key} : {value}")

    lines.append("&ETA")
    path = cfg.model_path(out_name or cfg.cas_file)
    path.write_text("\n".join(lines) + "\n")
    return path


def write_hotstart_cas(cfg: Config, duration: float,
                       out_name: str = "hotstart2d.cas") -> Path:
    """Derive a hotstart steering file from the *built* steady case.

    Rewrites ``cfg.cas_file`` (``steady2d.cas``) as a continuation run: the initial
    conditions block is switched from the pre-wetted/dry-start seed to
    ``PREVIOUS COMPUTATION FILE : <results_slf>`` (the steady run's own result), the
    results file is renamed so the run does not clobber its hotstart input, and
    ``DURATION`` is capped at *duration* - the simulated time the initial run needed
    to reach a sustained boundary-flux balance (``flux_convergence.find_steady_window``).
    Everything else - notably the constant ``PRESCRIBED FLOWRATES`` /
    ``PRESCRIBED ELEVATIONS`` - is carried over unchanged, so the hotstart drives the
    same steady Q and downstream H as the initial run.
    """
    steady_cas = cfg.model_path(cfg.cas_file)
    duration_s = float(math.ceil(duration)) if duration > 0 else float(duration)
    results = Path(cfg.results_slf)
    hot_results = f"{results.stem}-hotstart{results.suffix}"

    out_lines: list[str] = []
    have_previous = have_duration = False
    for line in steady_cas.read_text().splitlines():
        s = line.strip()
        if s.startswith("TITLE"):
            title = s.split(":", 1)[1].strip().strip("'")
            out_lines.append(f"TITLE : '{title} hotstart'")
        elif s.startswith("RESULTS FILE"):
            out_lines.append(f"RESULTS FILE : {hot_results}")
        elif s.startswith("DURATION"):
            out_lines.append(f"DURATION : {duration_s}")
            have_duration = True
        elif s.startswith("PREVIOUS COMPUTATION FILE FORMAT"):
            continue  # re-emitted right after the file line below
        elif s.startswith("PREVIOUS COMPUTATION FILE"):
            out_lines += [f"PREVIOUS COMPUTATION FILE : {cfg.results_slf}",
                          "PREVIOUS COMPUTATION FILE FORMAT : 'SERAFIN'"]
            have_previous = True
        elif s.startswith("INITIAL CONDITIONS") and not have_previous:
            # analytical (dry) initial conditions -> switch to the continuation
            out_lines += [f"PREVIOUS COMPUTATION FILE : {cfg.results_slf}",
                          "PREVIOUS COMPUTATION FILE FORMAT : 'SERAFIN'",
                          "INITIAL TIME SET TO ZERO : YES"]
            have_previous = True
        elif s.startswith("/ pre-wetted hotstart") or s.startswith("/ dry start"):
            out_lines.append("/ continuation of the steady initial run "
                             "(end time from the boundary-flux balance)")
        else:
            out_lines.append(line)

    if not have_previous:
        raise ValueError(
            f"{steady_cas} has neither PREVIOUS COMPUTATION FILE nor INITIAL "
            "CONDITIONS - cannot derive a hotstart continuation from it.")
    if not have_duration:
        # fixed-step steady case: DURATION still terminates the run (TELEMAC derives
        # the step count from it), so append it rather than touching the step keywords
        out_lines.insert(len(out_lines) - 1, f"DURATION : {duration_s}")

    path = cfg.model_path(out_name)
    path.write_text("\n".join(out_lines) + "\n")
    log.info("hotstart steering file %s (DURATION : %s s, continues %s -> %s)",
             path.name, duration_s, cfg.results_slf, hot_results)
    return path


def write_gaia_cas(cfg: Config) -> Path | None:
    """Write the GAIA steering file (bedload and/or suspended load).

    Emits a structurally valid GAIA .cas with the configured sediment classes and
    result file, then enables the transport modes set on ``cfg.morphodynamics``:

    * ``bedload`` -> ``BED LOAD FOR ALL SANDS : YES`` +
      ``BED-LOAD TRANSPORT FORMULA FOR ALL SANDS`` (``bedload_formula``, 1 = MPM);
    * ``suspended_load`` -> ``SUSPENSION FOR ALL SANDS : YES`` (GAIA transports the
      classes as TELEMAC tracers through the coupling).

    Sediment-transport tuning is left to the user / calibration (e.g. the Shields
    parameters or class diameters perturbed by HydroBayesCal via ``gaia*`` params).
    """
    if not cfg.morphodynamics.enabled:
        return None
    m = cfg.morphodynamics
    if not (m.bedload or m.suspended_load):
        log.warning("morphodynamics.enabled but neither bedload nor suspended_load is "
                    "on; the GAIA run will transport no sediment")
    classes = m.sediment_classes or [{"diameter": 0.001, "density": 2650}]
    diameters = ";".join(str(c.get("diameter", 0.001)) for c in classes)
    densities = ";".join(str(c.get("density", 2650)) for c in classes)
    shields = ";".join(str(c.get("shields", 0.047)) for c in classes)

    lines = [
        "/ GAIA steering file generated by hydromate",
        f"GAIA RESULTS FILE : {m.gaia_results_base}.slf",
        f"CLASSES SEDIMENT DIAMETERS : {diameters}",
        f"CLASSES SEDIMENT DENSITY : {densities}",
        f"CLASSES SHIELDS PARAMETERS : {shields}",
        "/",
        "/ TRANSPORT MODES",
        f"BED LOAD FOR ALL SANDS : {'YES' if m.bedload else 'NO'}",
        *([f"BED-LOAD TRANSPORT FORMULA FOR ALL SANDS : {m.bedload_formula}"]
          if m.bedload else []),
        # suspended sediment is transported as TELEMAC tracers by GAIA (the coupling
        # declares them on the hydro side); needs no NUMBER OF TRACERS in the .cas.
        f"SUSPENSION FOR ALL SANDS : {'YES' if m.suspended_load else 'NO'}",
    ]
    for key, value in m.extra_keywords.items():
        lines.append(f"{key} : {value}")
    lines.append("&ETA")
    path = cfg.model_path(cfg.gaia_cas)
    path.write_text("\n".join(lines) + "\n")
    return path
