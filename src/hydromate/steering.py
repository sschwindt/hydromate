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


def _liquid_node_mask(cfg: Config, mesh, kind: str, tol: float | None = None):
    """Boundary nodes at the ``inflow`` / ``outflow`` liquid line(s) of the case."""
    import numpy as np
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from hydromate import boundary as bnd

    on_boundary = np.asarray(mesh.ipobo) > 0
    lines = bnd._load_liquid_lines(cfg).get(kind)
    if lines is None:
        return on_boundary          # untagged: allow the whole outer boundary
    geom = unary_union(lines)
    if tol is None:
        tol = 2.0 * float(getattr(cfg.mesh, "floodplain_size", 1.0) or 1.0)
    idx = np.flatnonzero(on_boundary)
    # dtype matters: an empty list comprehension yields a float array, which cannot
    # index (this fires on a mesh whose ipobo is not set, e.g. one built in a test)
    close = np.array([geom.distance(Point(mesh.x[i], mesh.y[i])) <= tol for i in idx],
                     dtype=bool)
    out = np.zeros(len(mesh.x), dtype=bool)
    out[idx[close]] = True
    return out if out.any() else on_boundary


def spill_elevations(mesh, seed_mask):
    """Per-node **spill elevation** relative to the seed nodes: the lowest water
    level at which the node is hydraulically connected to them.

    This is Barnes et al.'s *priority flood* run on the mesh graph: starting from
    the seed nodes, the elevation of every node is raised to the highest bed it
    must cross on the cheapest path to a seed, i.e.

    .. math:: S_i = \\min_{\\text{paths } i \\to \\text{seed}} \\; \\max_{j \\in \\text{path}} z_j

    So ``S_i == z_i`` on ground that drains freely to a seed node, and ``S_i > z_i``
    behind a rim, where ``S_i`` is the elevation of the lowest saddle that must be
    overtopped. Seeded from the *outflow* it answers "can water here get out?";
    seeded from the *inflow*, "could the flow ever have got here?" - the pair defines
    the through-flowing corridor used by :func:`write_initial_conditions`.
    """
    import heapq

    import numpy as np
    from scipy.sparse import coo_matrix

    z = np.asarray(mesh.bottom, dtype=float)
    n = z.size
    t = np.asarray(mesh.triangles)
    e0 = np.concatenate([t[:, 0], t[:, 1], t[:, 2]])
    e1 = np.concatenate([t[:, 1], t[:, 2], t[:, 0]])
    rows = np.concatenate([e0, e1])
    cols = np.concatenate([e1, e0])
    adj = coo_matrix((np.ones(rows.size, dtype=np.int8), (rows, cols)),
                     shape=(n, n)).tocsr()
    indptr, indices = adj.indptr, adj.indices

    spill = np.full(n, np.inf)
    done = np.zeros(n, dtype=bool)
    heap = []
    for i in np.flatnonzero(seed_mask):
        spill[i] = z[i]
        heap.append((z[i], int(i)))
    heapq.heapify(heap)
    while heap:
        s, i = heapq.heappop(heap)
        if done[i]:
            continue
        done[i] = True
        for k in range(indptr[i], indptr[i + 1]):
            j = int(indices[k])
            if done[j]:
                continue
            cand = s if z[j] < s else z[j]
            if cand < spill[j]:
                spill[j] = cand
                heapq.heappush(heap, (cand, j))
    # nodes disconnected from any outlet can never drain at all
    spill[~np.isfinite(spill)] = np.inf
    return spill


def _channel_centerline(cfg: Config):
    """The channel centerline as a single LineString in the project CRS."""
    import geopandas as gpd
    from shapely.ops import linemerge, unary_union

    gdf = gpd.read_file(cfg.geodata.channel_centerline)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    merged = unary_union(gdf.geometry.values)
    line = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
    if line.geom_type == "MultiLineString":          # disjoint parts: take longest
        line = max(line.geoms, key=lambda g: g.length)
    return line


def _centerline_arclength(cfg: Config, mesh):
    """Per-node arc length: the along-reach distance of the nearest channel-
    centerline vertex, used to order channel nodes from upstream to downstream."""
    import numpy as np
    from scipy.spatial import cKDTree

    line = _channel_centerline(cfg)
    n = max(2, int(line.length / 2.0))
    sline = np.linspace(0.0, line.length, n)
    cpts = np.array([[line.interpolate(d).x, line.interpolate(d).y] for d in sline])
    _, idx = cKDTree(cpts).query(np.column_stack([mesh.x, mesh.y]))
    return sline[idx]


def _node_roughness(cfg: Config, mesh):
    """Per-node Nikuradse ``ks`` [m], from the roughness zones when available.

    ``mesh.roughness`` carries the ``geodata.roughness_table`` value of the zone each
    node falls in (set by :func:`hydromate.mesh.interpolate_roughness`). Without
    roughness zones the lateral-boundary roughness is converted to an equivalent ks,
    the same fallback :func:`hydromate.rating.synthesize_outflow_rating_from_section`
    uses.
    """
    import numpy as np

    from hydromate.rating import _resolve_n

    if getattr(mesh, "roughness", None) is not None:
        ks = np.asarray(mesh.roughness, dtype=float)
        usable = np.isfinite(ks) & (ks > 0)
        if usable.any():   # fill any gap with the median of the usable values
            return np.where(usable, ks, float(np.median(ks[usable])))
    law, coef = cfg.friction.boundary_law, cfg.friction.boundary_coefficient
    n = _resolve_n(coef if law == 4 else None, coef if law == 3 else None)
    return np.full(mesh.x.shape, (n / 0.0474) ** 6)


#: bounds on the per-transect bed slope used for the normal-depth seed [-]
PREWET_SLOPE_LIMITS = (0.002, 0.03)


def _prewet_discharge(cfg: Config) -> float | None:
    """Discharge the normal-depth pre-wet is sized for, or None if unavailable.

    ``workflow.resolve_discharge`` raises when a case has neither a prescribed
    flowrate nor an inflow series; the seed then falls back to constant mode rather
    than failing the build, since a hotstart state only has to be a starting point.
    """
    from hydromate.workflow import resolve_discharge

    try:
        return float(resolve_discharge(cfg))
    except Exception as exc:
        log.debug("no discharge for the normal-depth pre-wet (%s); "
                  "falling back to the constant-depth seed", exc)
        return None


def _normal_depth_prewet_depth(cfg: Config, mesh, mask, discharge):
    """Pre-wet water depth from the **normal-flow stage of the real cross-sections**.

    Every ``initialization.prewet_bin_spacing`` metres along the channel centerline a
    perpendicular transect of ``prewet_transect_half_width`` half-width is cut,
    sampled at 0.5 m, clipped to the ``*channel*`` mesh zones and given the bed and
    Nikuradse ``ks`` of the nearest mesh node. The transect's **normal stage** at the
    case *discharge* follows from :func:`hydromate.rating.stage_for_discharge` (the
    same Keulegan conveyance inversion that builds the outflow rating), with the local
    bed slope taken from the gradient of the transect thalwegs - not one slope for the
    whole reach, which is wrong where the local gradient varies by a factor of four.

    The seeded surface is then

    .. math:: wl(s) = z_{thalweg}(s) + f \\cdot (z_{normal}(s) - z_{thalweg}(s))

    with the fill factor ``initialization.prewet_fill`` (< 1 by design), and the depth
    is ``max(wl - bed, 0)`` on the channel nodes.

    Why a *conveyance* stage rather than the older "low bed percentile + a fixed
    depth": on a braided reach that percentile sits well above the thalweg, so the
    seeded surface lands above the surface the run converges to and floods bar tops
    and bank shelves. That water then has nowhere to go - a 2D model has neither
    infiltration nor evaporation - and it survives as immobile film for the whole run.
    Referencing the stage that actually carries the discharge removes the cause.
    """
    import numpy as np
    import shapely
    from scipy.spatial import cKDTree

    from hydromate import mesh as mesh_mod
    from hydromate.rating import stage_for_discharge

    init = cfg.initialization
    line = _channel_centerline(cfg)
    channel = mesh_mod._channel_union(cfg)
    spacing = float(init.prewet_bin_spacing)
    half = float(init.prewet_transect_half_width)
    sample = 0.5

    bottom = np.asarray(mesh.bottom, dtype=float)
    ks_node = _node_roughness(cfg, mesh)
    tree = cKDTree(np.column_stack([mesh.x, mesh.y]))
    offsets = np.arange(-half, half + sample, sample)

    centers = np.arange(spacing / 2.0, max(line.length, spacing), spacing)
    sections: list[tuple | None] = []
    thalweg = np.full(centers.size, np.nan)
    for j, c in enumerate(centers):
        p = line.interpolate(c)
        a = line.interpolate(max(0.0, c - 2.0))
        b = line.interpolate(min(line.length, c + 2.0))
        tx, ty = b.x - a.x, b.y - a.y
        norm = float(np.hypot(tx, ty)) or 1.0
        nx, ny = -ty / norm, tx / norm            # left normal to the centerline
        px, py = p.x + nx * offsets, p.y + ny * offsets
        inside = np.asarray(shapely.contains(channel, shapely.points(px, py)))
        if inside.sum() < 10:                     # transect barely meets the channel
            sections.append(None)
            continue
        _, idx = tree.query(np.column_stack([px[inside], py[inside]]))
        sections.append((offsets[inside], bottom[idx], ks_node[idx]))
        thalweg[j] = float(bottom[idx].min())

    good = np.isfinite(thalweg)
    if good.sum() < 2:
        raise ValueError(
            "the channel centerline yields no usable cross-section for the "
            "normal-depth pre-wet (check geodata.channel_centerline against "
            "geodata.mesh_zones)"
        )
    thalweg = np.interp(centers, centers[good], thalweg[good])
    # smooth the thalweg profile before differentiating it: a raw per-transect
    # minimum is pitted, and its gradient would be noise
    k = 2
    thalweg = np.convolve(np.pad(thalweg, k, mode="edge"),
                          np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    slope = np.clip(np.abs(np.gradient(thalweg, centers)), *PREWET_SLOPE_LIMITS)

    stage = np.full(centers.size, np.nan)
    for j, sec in enumerate(sections):
        if sec is None:
            continue
        station, bed, ks = sec
        stage[j] = stage_for_discharge(discharge, station=station, bed=bed, ks=ks,
                                       slope=float(slope[j]))
    ok = np.isfinite(stage)
    if not ok.any():
        raise ValueError("no cross-section along the centerline could be rated for "
                         f"Q={discharge:g} m3/s")
    stage = np.interp(centers, centers[ok], stage[ok])
    stage = np.maximum(stage, thalweg)

    fill = float(init.prewet_fill)
    wl_bin = thalweg + fill * (stage - thalweg)
    s_node = _centerline_arclength(cfg, mesh)
    wl = np.interp(s_node, centers, wl_bin)
    depth = np.where(mask, np.maximum(wl - bottom, 0.0), 0.0)
    log.info("  normal-depth pre-wet: %d cross-sections, normal depth over thalweg "
             "%.2f m mean (Q=%.3f m3/s, slope %.4f..%.4f), seeded at fill %.2f "
             "-> %.2f m mean over thalweg",
             int(ok.sum()), float(np.mean(stage - thalweg)), discharge,
             float(slope.min()), float(slope.max()), fill,
             float(np.mean(wl_bin - thalweg)))
    return depth


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

    The seeded water surface comes from ``initialization.prewet_mode``:

    * ``normal-depth`` (default): the normal-flow stage of the real cross-sections at
      the case discharge, scaled by ``prewet_fill``
      (:func:`_normal_depth_prewet_depth`). The seeded surface then tracks the surface
      the run converges to, so no water is laid down above it.
    * ``constant``: the older longitudinally smoothed "low bed percentile +
      ``prewet_depth``" surface (:func:`_longitudinal_prewet_depth`). Also the
      automatic fallback when there is no centerline or no discharge to size the
      normal depth with - a plain constant depth would make the seeded surface as
      jagged as the bed and leave a depth "dam" at every channel/floodplain edge,
      which diverges at t=0 on steep terrain.

    Whatever the mode, nodes that would receive less than
    ``initialization.prewet_min_depth`` are left dry: a feathered seed margin carries
    no flow worth hotstarting and is exactly what turns into stagnant film.

    **Drainable seeding** (``initialization.drainable_prewet``, on by default): the
    depth is measured from the node's *spill* elevation (:func:`spill_elevations`)
    rather than from the bed, so no water is ever seeded below the rim of a closed
    depression. Seeding a bowl the flow would never fill leaves a pond that cannot
    drain - TELEMAC-2D has no infiltration or evaporation - and it survives to the
    end of the run as a stagnant, wrongly wetted alcove or side channel. Referencing
    the spill elevation removes exactly that water and nothing else: on ground that
    drains freely the spill elevation *is* the bed, so the open channel is seeded
    unchanged.
    """
    import numpy as np

    from hydromate import mesh as mesh_mod
    from hydromate import selafin

    init = cfg.initialization
    depth_val = float(init.prewet_depth)
    mask = mesh_mod.channel_node_mask(cfg, mesh)
    discharge = _prewet_discharge(cfg)
    depth = how = None
    if init.prewet_mode == "normal-depth" and cfg.geodata.channel_centerline is not None \
            and discharge is not None:
        try:
            depth = _normal_depth_prewet_depth(cfg, mesh, mask, discharge)
            how = (f"normal-depth surface at Q={discharge:g} m3/s, "
                   f"fill {init.prewet_fill:g}")
        except Exception as exc:  # noqa: BLE001 - a seed is only a starting state
            # A reach too short to section, or a centerline that misses the channel
            # zones, must not fail the build: fall back to the older seed and say so.
            log.warning("  normal-depth pre-wet unavailable (%s: %s); falling back to "
                        "the constant-depth seed", type(exc).__name__, exc)
    if depth is None and cfg.geodata.channel_centerline is not None:
        s_node = _centerline_arclength(cfg, mesh)
        depth = _longitudinal_prewet_depth(s_node, mesh.bottom, mask, depth_val)
        how = "smoothed longitudinal surface"
    elif depth is None:
        depth = np.where(mask, depth_val, 0.0)
        how = "constant depth (no centerline)"
    # WATER TABLE under the porous patch. Computed from the seeded surface just built
    # (the plane needs the channel level at each internal line) and re-applied AFTER
    # both filters below: a groundwater-fed pool is not trapped seed water, and the
    # filters exist precisely to remove water that has no source. This one has one.
    # Without it such a pool stays dry for the whole run - it sits on a bar ABOVE the
    # channel, so no surface flow can ever reach it.
    supported = np.zeros(depth.shape, dtype=float)
    if cfg.percolation.water_table == "phreatic":
        from hydromate import watertable

        plane = watertable.fit_phreatic_plane(cfg, mesh, surface=mesh.bottom + depth)
        if plane is not None:
            patch = watertable.patch_node_mask(cfg, mesh)
            supported = watertable.water_table_depth(plane, mesh, patch)
            area = _nodal_areas(mesh)
            log.info("  %s", watertable.describe(plane, mesh, patch, area))
            how += ", water table"

    floor = float(init.prewet_min_depth)
    if floor > 0.0:
        thin = (depth > 0.0) & (depth < floor)
        if thin.any():
            log.info("  dropped %d seeded nodes thinner than the %.2f m floor "
                     "(a feathered seed margin becomes stagnant film)",
                     int(thin.sum()), floor)
            depth = np.where(thin, 0.0, depth)
        how += f", >= {floor:g} m"
    if cfg.initialization.drainable_prewet:
        bottom = np.asarray(mesh.bottom, dtype=float)
        tol = float(cfg.initialization.drainable_tolerance)
        # Seed only ground that drains FREELY to the outflow, i.e. whose spill
        # elevation is its own bed (within *tol*, a DEM-noise allowance). Anywhere
        # behind a rim is left dry.
        #
        # Merely being *connected* at the seeded level is not enough: such a column
        # drains only down to the rim and the remainder then stays put for the whole
        # run, because a 2D model has neither infiltration nor evaporation. On this
        # case that residue is ~314 m3 - which is precisely the ~322 m3 of water
        # found standing still at under 2 cm/s in the previous result. Under-seeding
        # a genuine channel pool costs nothing by comparison: the flow refills it
        # within seconds of the start.
        spill = spill_elevations(mesh, _liquid_node_mask(cfg, mesh, "outflow"))
        free = spill <= bottom + tol
        drainable = np.where(free, depth, 0.0)
        seeded = depth > 0.01
        dropped = int((seeded & ~free).sum())
        residue = float(np.maximum(spill - bottom, 0.0)[free & (drainable > 0.01)].sum())
        log.info("  drainable seeding (tol %.3f m): dropped %d of %d seeded nodes "
                 "(%.1f%% of the seeded depth-sum) that sit behind a rim and could "
                 "never drain; residual trapped depth-sum %.2f m",
                 tol, dropped, int(seeded.sum()),
                 100.0 * (1.0 - drainable.sum() / max(depth.sum(), 1e-9)), residue)
        depth = drainable
        how += ", drainable"

    # re-impose the water table: whatever the filters removed, ground below the bar's
    # phreatic surface is genuinely wet and stays seeded to it
    if supported.any():
        raised = supported > depth
        if raised.any():
            log.info("  water table: restored %d node(s) the seed filters had emptied "
                     "(%.1f m3) - a groundwater-fed pool has a source, so the "
                     "drainable and min-depth filters must not apply to it",
                     int(raised.sum()),
                     float(((supported - depth)[raised] * _nodal_areas(mesh)[raised]).sum()))
        depth = np.maximum(depth, supported)

    # MANDATORY, and applied after every filter: TELEMAC's DEBIMP distributes the
    # prescribed discharge over the inflow section by scaling a velocity profile with
    # Q/Q1, Q1 proportional to the integral of H along that section. A dry inflow
    # gives Q1 = 0 and the run aborts at t=0 with "DEBIMP: PROBLEM ON BOUNDARY
    # NUMBER n". The seed filters above have no reason to keep that section wet - the
    # normal-depth seed reaches its shallowest exactly at the upstream end, and both
    # the min-depth floor and the drainable test can then empty it - so the inflow
    # plug is re-imposed here, the same one write_dry_start_conditions lays down.
    plug = _inflow_plug_mask(cfg, mesh)
    if plug is not None:
        seed = float(init.dry_start_depth)
        short = plug & (depth < seed)
        if short.any():
            log.info("  inflow plug: raised %d node(s) within %.1f m of the inflow to "
                     "%.2f m (DEBIMP aborts on a dry inflow cross-section)",
                     int(short.sum()), _inflow_plug_extent(cfg), seed)
            depth = np.where(short, seed, depth)
            how += ", inflow plug"

    path = cfg.model_path(cfg.ic_slf)
    selafin.write_initial_state(
        path, x=mesh.x, y=mesh.y, ikle=mesh.triangles + 1, ipobo=mesh.ipobo,
        depth=depth, title=f"{cfg.name} initial conditions",
    )
    area = _nodal_areas(mesh)
    seeded = depth > 0.0
    log.info("  pre-wet %d/%d channel nodes (%s): %.0f m2 wetted, %.0f m3 seeded, "
             "mean depth %.2f m, max %.2f m -> %s",
             int(seeded.sum()), mask.size, how, float(area[seeded].sum()),
             float((depth * area).sum()),
             float(depth[seeded].mean()) if seeded.any() else 0.0,
             float(depth.max()), path.name)
    return path


def _nodal_areas(mesh):
    """Per-node share of the mesh area [m2] (a third of each incident triangle)."""
    import numpy as np

    xy = np.column_stack([mesh.x, mesh.y])
    tri = xy[mesh.triangles]
    twice = ((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
             - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    elem = np.abs(twice) / 2.0
    area = np.zeros(mesh.x.size, dtype=float)
    np.add.at(area, mesh.triangles.ravel(), np.repeat(elem / 3.0, 3))
    return area


def _inflow_plug_extent(cfg: Config) -> float:
    """How far from the inflow line(s) the water plug reaches [m]."""
    init = cfg.initialization
    if init.dry_start_extent is not None:
        return float(init.dry_start_extent)
    return (max(cfg.mesh.channel_size, cfg.mesh.floodplain_size)
            * cfg.mesh.size_scale * 5.0)


def _inflow_plug_mask(cfg: Config, mesh):
    """Boolean (NPOIN,) flag of the nodes forming the inflow water plug, or None
    when the case has no inflow line."""
    import numpy as np
    from shapely import contains_xy

    from hydromate import boundary

    try:
        inflow = boundary._load_liquid_lines(cfg).get("inflow")
    except Exception as exc:  # noqa: BLE001 - a seed must not fail on a bad layer
        log.debug("no inflow plug (%s: %s)", type(exc).__name__, exc)
        return None
    if inflow is None:
        return None
    extent = _inflow_plug_extent(cfg)
    return np.asarray(contains_xy(inflow.buffer(extent), mesh.x, mesh.y), dtype=bool)


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

    from hydromate import selafin

    plug = _inflow_plug_mask(cfg, mesh)
    if plug is None:
        return None
    seed = float(cfg.initialization.dry_start_depth)
    depth = np.where(plug, seed, 0.0)
    extent = _inflow_plug_extent(cfg)
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

    *inflow_q* is the **total** reach discharge. When each inflow boundary carries
    its **own** ``LiquidBoundary.discharge`` (per-line values from the layer's flow
    column, e.g. two upstream inflows of 1.6 and 0.8 m3/s), those are prescribed
    directly. Otherwise *inflow_q* is distributed across the inflow boundaries in
    proportion to their node count (a width/conveyance proxy) so the total prescribed
    inflow stays *inflow_q* - prescribing the full Q on each would multiply the
    supplied discharge and flood the domain.
    """
    free_outflow = cfg.boundaries.outflow_condition == "free"
    inflows = [b for b in liquids if b.kind == "inflow"]
    per_line = bool(inflows) and all(b.discharge is not None for b in inflows)
    inflow_nodes = sum(b.n_nodes for b in inflows) or 1
    flow, elev, prof = [], [], []
    for lb in sorted(liquids, key=lambda b: b.index):
        if lb.kind == "inflow":
            share = lb.discharge if per_line else inflow_q * lb.n_nodes / inflow_nodes
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
    per_line = bool(inflows) and all(b.discharge is not None for b in inflows)
    disch_total = sum(b.discharge for b in inflows) if per_line else 0.0
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
                # split the total hydrograph value qt by the per-line discharge ratio
                # when available, else by node share (matching the steady prescription)
                if per_line and disch_total > 0:
                    row.append(f"{qt * lb.discharge / disch_total:.4f}")
                else:
                    row.append(f"{qt * lb.n_nodes / inflow_nodes:.4f}")
            elif not free_outflow:
                row.append(f"{(outflow_wse_fn(qt) if outflow_wse_fn else 0.0):.4f}")
        lines.append(" ".join(row))
    path = cfg.model_path(cfg.liquid_boundaries_file)
    path.write_text("\n".join(lines) + "\n")
    return path


def _region_coords(region) -> list[tuple[float, float]]:
    """Exterior vertices of a source region, without the closing duplicate."""
    return [(float(x), float(y)) for x, y in list(region.polygon.exterior.coords)[:-1]]


def write_source_regions(cfg: Config, regions: list) -> Path:
    """Write the TELEMAC ``SOURCE REGIONS DATA FILE`` (``source-regions.txt``).

    Format (``read_source_data.f``): ``#`` comment lines; per region a header line
    ``X(i)   Y(i)`` followed by one ``x y`` vertex pair per line; a ``#`` comment
    line terminates each region block. The region order defines TELEMAC's region
    numbering and must match the order of ``WATER DISCHARGE OF SOURCES`` in the
    ``.cas`` (both come from the same *regions* list here). TELEMAC assigns every
    mesh node inside a polygon to its region and spreads the region discharge
    uniformly over the enclosed area (``telemac2d_init.F`` / ``prosou.f``).

    **No blank lines, and no line may start with a space or tab.**
    ``read_source_data.f`` skips leading whitespace with a ``GO TO 2`` that jumps
    back onto ``2 CONTINUE`` - which re-runs ``IDEB=1``. Any whitespace-only line
    reaching that block therefore loops forever: the solver spins at 100% CPU
    right after ``RESCUE : SPALART ALLMARAS``, with no error, no time step and no
    end (only ``MAXIMUM NUMBER OF TIME STEPS``, unused here, would ever stop it).
    Hence the ``#`` block terminators rather than blank separator lines.
    """
    lines = ["# hydromate: internal source/sink regions (losing-gaining reach)"]
    for i, region in enumerate(regions, start=1):
        lines += [
            (f"# region {i}: {region.name} ({region.discharge:+g} m3/s, "
             f"{region.area:.0f} m2)"),
            f"X({i})   Y({i})",
        ]
        lines += [f"{x:.3f} {y:.3f}" for x, y in _region_coords(region)]
        lines.append("#")   # terminates the block; never a blank line (see above)
    path = cfg.model_path(cfg.source_regions_file)
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
              hotstart_note: str | None = None,
              source_regions: "list | None" = None) -> Path:
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
    *source_regions* (a :class:`hydromate.boundary.InternalSourceRegion` list) adds
    the internal losing/gaining exchange - as SOURCE REGIONS keywords referencing
    the file written by :func:`write_source_regions`, or, in ``percolation.mode:
    fortran``, as the FORTRAN FILE + RAIN keywords for the generated USER_RAIN
    routine (see :mod:`hydromate.fortran`).
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
        # clip H/U/V/T so a local spike can't cascade to NaN; a divergence guard.
        # config-driven (hydrodynamics.control_of_limits) so switching it off for
        # a diagnosis is reproducible rather than a hand-edit lost on rebuild.
        *(["CONTROL OF LIMITS : YES",
           "LIMIT VALUES : -1000;9000;-1000;1000;-1000;1000;-1000;1000"]
          if h.control_of_limits else ["/ CONTROL OF LIMITS disabled via config"]),
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
    ]

    if (cfg.gain_lose.active and cfg.gain_lose.implementation == "fortran"
            and source_regions):
        # percolation via a generated USER_RAIN routine (see hydromate.fortran):
        # depth-limited withdrawal over the patch, mass-exact reinjection at the
        # gaining line. PLUIE needs RAIN OR EVAPORATION active (base rate 0; the
        # routine assigns the nodal rates itself), and the compiled routine comes
        # from the FORTRAN FILE folder. NO SOURCE REGIONS keywords here - the
        # exchange must not be double-counted.
        tags = ", ".join(f"{r.name} {r.discharge:+g}" for r in source_regions)
        lines += [
            "/",
            f"/ INTERNAL SOURCES / SINKS via USER_RAIN percolation ({tags} m3/s)",
            f"FORTRAN FILE : '{cfg.user_fortran_dir}'",
            "RAIN OR EVAPORATION : YES",
            "RAIN OR EVAPORATION IN MM PER DAY : 0.",
        ]
    elif source_regions:
        # internal source/sink REGIONS for a losing-gaining reach: a 2D model has
        # no subsurface, so surface water that leaves through a losing line and
        # returns through a gaining line is a withdrawal (-Q) and an injection (+Q),
        # each spread by TELEMAC over the mesh nodes inside a polygon region
        # (SOURCE REGIONS DATA FILE; Q/area as a depth rate - far gentler per node
        # than point sources). One discharge value per region also keeps the steering
        # file inside DAMOCLES' 72-column line buffer, which a point source per node
        # would overflow. No source velocity is prescribed, so the exchange takes the
        # local flow velocity. NOTE: never emit ABSCISSAE/ORDINATES OF SOURCES
        # together with the region file - the coordinate route would shadow it
        # (lecdon precedence).
        #
        # TYPE OF SOURCES MUST BE 1 ("normal") FOR REGIONS, never 2 ("Dirac").
        # prosou.f:528 branches on OPTSOU inside the per-node region loop:
        #   OPTSOU=1  SMH(II) += DSCE/AREA_P   -> integrates to exactly DSCE
        #   OPTSOU=2  SMH(II) += DSCE          -> the FULL discharge at EVERY node,
        #                                         i.e. n_nodes x DSCE injected
        # Dirac is only correct for a POINT source (a single node). On the isar-2025
        # 221/326-node regions it would have withdrawn 14.4 and injected 21.2 m3/s
        # against a 2.4 m3/s inflow. OPTSOU=1 is in turn rejected by the finite-volume
        # kernel (prosou message 323), so region sources need the FE kernel.
        qs = ";".join(f"{r.discharge:g}" for r in source_regions)
        tags = ", ".join(f"{r.name} {r.discharge:+g} m3/s over {r.n_nodes} node(s)"
                         f" / {r.area:.0f} m2" for r in source_regions)
        # MAXSCE counts REGIONS, not the nodes they capture: point_telemac2d.f sizes
        # PT_IN_POLY(MAXSCE, NPOIN), so an oversized MAXSCE wastes MAXSCE*NPOIN ints
        # (721 on a 231k-node mesh = 666 MB) for nothing. MAXIMUM NUMBER OF POINTS FOR
        # SOURCES REGIONS (MAXPTSCE, default 10) must in turn cover the polygon with
        # the most vertices (the region file drops the closing duplicate, matching
        # write_source_regions) or TELEMAC overruns its array - and also the region
        # COUNT, since PTS_REG is allocated MAXPTSCE but indexed by region number.
        max_vertices = max(len(_region_coords(r)) for r in source_regions)
        lines += [
            "/",
            "/ INTERNAL SOURCES / SINKS (losing-gaining reach, spread over regions):",
            f"/ {tags}",
            f"SOURCE REGIONS DATA FILE : {cfg.source_regions_file}",
            f"MAXIMUM NUMBER OF SOURCES : {len(source_regions)}",
            ("MAXIMUM NUMBER OF POINTS FOR SOURCES REGIONS : "
             f"{max(max_vertices, len(source_regions), 10)}"),
            f"WATER DISCHARGE OF SOURCES : {qs}",
            "TYPE OF SOURCES : 1",
        ]
        # finite_volumes lives on cfg.hydrodynamics, not on cfg - reading it off cfg
        # made this guard dead code (getattr always fell back to False).
        if h.finite_volumes:
            log.warning(
                "internal source REGIONS need TYPE OF SOURCES 1, which the "
                "finite-volume kernel rejects (prosou: 'ONLY SOURCES WITH DIRAC "
                "OPTION'). Set hydrodynamics.finite_volumes: false, or the run will "
                "abort at the first time step.")

    lines += [
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
        *([f"ACCURACY OF SPALART-ALLMARAS : {h.turbulence_solver_accuracy}",
           # the Spalart-Allmaras transport solve shares the k-epsilon iteration
           # budget keyword; TELEMAC's default 50 is too few on an ill-conditioned
           # (dry-start / thin-film) domain - the "GRACJG: EXCEEDING MAXIMUM
           # ITERATIONS 50" storm - so raise it here too.
           f"MAXIMUM NUMBER OF ITERATIONS FOR K AND EPSILON : {h.max_keps_iterations}"]
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


# GAIA graphic output: velocities/depth/free-surface/bottom for context plus the
# morphodynamic quantities - bed evolution (E), bed shear stress (TOB) and the mean
# grain size (M) needed to interpret bed change and calibrate against a DoD.
_GAIA_DEFAULT_PRINTOUTS = "'U,V,S,H,B,E,TOB,M'"


def write_gaia_cas(cfg: Config) -> Path | None:
    """Write the GAIA steering file (bedload and/or suspended load + bed processes).

    Emits a structurally valid GAIA ``.cas`` - referencing the same ``GEOMETRY FILE``
    and ``BOUNDARY CONDITIONS FILE`` as the coupled hydrodynamic run (GAIA needs both
    even in coupled mode) - then enables the transport modes and bed-process
    capacities set on ``cfg.morphodynamics``:

    * ``bedload`` -> ``BED LOAD FOR ALL SANDS : YES`` +
      ``BED-LOAD TRANSPORT FORMULA FOR ALL SANDS`` (``bedload_formula``, 1 = MPM);
    * ``suspended_load`` -> ``SUSPENSION FOR ALL SANDS : YES`` (GAIA transports the
      classes as TELEMAC tracers through the coupling);
    * ``MORPHOLOGICAL FACTOR`` accelerates bed evolution over a hydrograph;
    * ``SLOPE EFFECT`` (+ formula + friction angle) steers bedload down transverse
      bed slopes; ``SECONDARY CURRENTS`` (+ alpha) adds the spiral-flow deviation in
      bends; ``NUMBER OF LAYERS FOR INITIAL STRATIFICATION`` / ``ACTIVE LAYER
      THICKNESS`` set the bed stratigraphy;
    * ``PRESCRIBED SOLID DISCHARGES`` imposes a sediment supply per liquid boundary
      (else GAIA uses its equilibrium inflow).

    The same file is shared by the steady case and by both hydrograph runs
    (``unsteady2d.cas`` / ``unsteady3d.cas``) - see :class:`hydromate.config.Morphodynamics`.
    Sediment-transport tuning is otherwise left to the user / calibration (e.g. the
    Shields parameters or class diameters perturbed by HydroBayesCal via ``gaia*``).
    """
    if not cfg.morphodynamics.enabled:
        return None
    m = cfg.morphodynamics
    if not (m.bedload or m.suspended_load):
        log.warning("morphodynamics.enabled but neither bedload nor suspended_load is "
                    "on; the GAIA run will transport no sediment")
    classes = m.sediment_classes or [{"diameter": 0.001, "density": 2650}]
    n_classes = len(classes)
    diameters = ";".join(str(c.get("diameter", 0.001)) for c in classes)
    densities = ";".join(str(c.get("density", 2650)) for c in classes)
    shields = ";".join(str(c.get("shields", 0.047)) for c in classes)
    # CLASSES TYPE OF SEDIMENT is per class: repeat the configured type for each.
    types = ";".join([f"'{m.sediment_type}'"] * n_classes)

    lines = [
        "/ GAIA steering file generated by hydromate",
        "/ (coupled morphodynamics; the driver .cas holds COUPLING WITH : 'GAIA')",
        f"GEOMETRY FILE : {cfg.geometry_slf}",
        f"BOUNDARY CONDITIONS FILE : {cfg.boundary_cli}",
        f"RESULTS FILE : {m.gaia_results_base}.slf",
        f"VARIABLES FOR GRAPHIC PRINTOUTS : {m.graphic_printouts or _GAIA_DEFAULT_PRINTOUTS}",
        f"MASS-BALANCE : {'YES' if m.mass_balance else 'NO'}",
        f"MORPHOLOGICAL FACTOR : {m.morphological_factor:g}",
        "/",
        "/ SEDIMENT CLASSES",
        f"CLASSES TYPE OF SEDIMENT : {types}",
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
        "/",
        "/ BED PROCESSES",
        f"SLOPE EFFECT : {'YES' if m.slope_effect else 'NO'}",
        *([f"FORMULA FOR SLOPE EFFECT : {m.slope_formula}",
           f"FRICTION ANGLE OF THE SEDIMENT : {m.friction_angle:g}"]
          if m.slope_effect else []),
        f"NUMBER OF LAYERS FOR INITIAL STRATIFICATION : {m.bed_layers}",
        *([f"ACTIVE LAYER THICKNESS : {m.active_layer_thickness:g}"]
          if m.active_layer_thickness is not None else []),
    ]
    if m.secondary_currents:
        # spiral-flow deviation of bedload in bends (needs the driver's secondary
        # currents; the alpha coefficient scales the deviation intensity).
        lines += ["/", "/ SECONDARY CURRENTS (bend-driven bedload deviation)",
                  "SECONDARY CURRENTS : YES",
                  f"SECONDARY CURRENTS ALPHA COEFFICIENT : {m.secondary_currents_alpha:g}"]
    if m.prescribed_solid_discharges is not None:
        # one value per liquid boundary, same FRONT2 order as PRESCRIBED FLOWRATES.
        qs = ";".join(f"{q:g}" for q in m.prescribed_solid_discharges)
        lines += ["/", "/ SEDIMENT SUPPLY (per liquid boundary, FRONT2 order)",
                  f"PRESCRIBED SOLID DISCHARGES : {qs}"]
    for key, value in m.extra_keywords.items():
        lines.append(f"{key} : {value}")
    lines.append("&ETA")
    path = cfg.model_path(cfg.gaia_cas)
    path.write_text("\n".join(lines) + "\n")
    return path
