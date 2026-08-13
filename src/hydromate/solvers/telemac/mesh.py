"""Stage 2 - mesh generation, bathymetry, and SELAFIN geometry.

Builds a triangular mesh with gmsh from the ROI boundary polygon and the
breaklines, with per-region (MATID) size refinement, then:

* extracts nodes + triangles,
* walks the mesh boundary to build the ordered contour (drives both TELEMAC's
  IPOBO array and the ``.cli`` row order - they must agree),
* interpolates the clipped DEM onto the nodes (bathymetry),
* assigns each element a MATID (friction zone),
* writes the geometry ``.slf`` via :mod:`hydromate.core.selafin`.

The boundary contour and the per-node/element classification are returned so the
boundary-condition stage can tag inflow/outflow nodes consistently.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hydromate.config import Config
from hydromate.core import geodata
from hydromate.core.geodata import dataset
from hydromate.core.raster import sample_raster_at
from hydromate.core import selafin
from hydromate.logsetup import log_step

log = logging.getLogger("hydromate")


@dataclass
class Mesh:
    x: np.ndarray              # (NPOIN,)
    y: np.ndarray              # (NPOIN,)
    triangles: np.ndarray      # (NELEM, 3) 0-based node indices
    bottom: np.ndarray         # (NPOIN,) bed elevation
    ipobo: np.ndarray          # (NPOIN,) TELEMAC boundary numbering
    boundary_nodes: np.ndarray # ordered 0-based node indices along the contour
    element_matid: np.ndarray  # (NELEM,) MATID per element
    node_matid: np.ndarray     # (NPOIN,) MATID per node (-> FRIC_ID in geometry)
    boundary_loops: np.ndarray | None = None  # per-loop node counts in boundary_nodes
    roughness: np.ndarray | None = None  # (NPOIN,) per-node roughness value (e.g. ks)
    quality: object | None = None        # mesh_quality.QualityReport (set by build_mesh)

    @property
    def npoin(self) -> int:
        return self.x.size

    @property
    def nelem(self) -> int:
        return self.triangles.shape[0]


# --------------------------------------------------------------------------- #
# gmsh geometry + size fields
# --------------------------------------------------------------------------- #


def roi_polygon(cfg: Config):
    """Return the ROI as a shapely Polygon (polygonising lines if needed)."""
    return dataset(cfg).roi_polygon()


def _read_lines(path: Path, crs_epsg: int):
    """Coordinate lists of every line in *path*, in the project CRS."""
    import geopandas as gpd

    gdf = gpd.read_file(path)
    if gdf.crs and gdf.crs.to_epsg() != crs_epsg:
        gdf = gdf.to_crs(epsg=crs_epsg)
    coords = []
    for geom in gdf.geometry.values:
        if geom.geom_type == "LineString":
            coords.append(list(geom.coords))
        elif geom.geom_type == "MultiLineString":
            coords.extend(list(g.coords) for g in geom.geoms)
    return coords


def _read_region_seeds(cfg: Config):
    """Return (xy array, matid array) of region seed points, or (None, None)."""
    return dataset(cfg).region_seeds()


def _build_gmsh(cfg: Config, *, bg_budget: int = 40000, aniso_relax: float = 1.0):
    import gmsh

    m = cfg.mesh
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add(cfg.name)
    geo = gmsh.model.geo

    poly = roi_polygon(cfg)

    def add_ring(coords):
        pts = list(coords)
        if pts[0] == pts[-1]:
            pts = pts[:-1]
        tags = [geo.addPoint(px, py, 0.0, m.default_size * m.size_scale)
                for px, py in pts]
        lines = [geo.addLine(tags[i], tags[(i + 1) % len(tags)]) for i in range(len(tags))]
        return geo.addCurveLoop(lines)

    outer = add_ring(poly.exterior.coords)
    holes = [add_ring(r.coords) for r in poly.interiors]
    surface = geo.addPlaneSurface([outer, *holes])

    # breaklines as embedded constraint lines
    embedded_lines: list[int] = []
    if cfg.geodata.breaklines is not None:
        for coords in _read_lines(Path(cfg.geodata.breaklines), cfg.crs_epsg):
            pts = [geo.addPoint(px, py, 0.0, m.breakline_size * m.size_scale)
                   for px, py in coords]
            for i in range(len(pts) - 1):
                embedded_lines.append(geo.addLine(pts[i], pts[i + 1]))

    geo.synchronize()
    if embedded_lines:
        gmsh.model.mesh.embed(1, embedded_lines, 2, surface)

    if _anisotropic_enabled(cfg):
        _anisotropic_size_field(cfg, poly, bg_budget=bg_budget, aniso_relax=aniso_relax)
    else:
        _size_fields(cfg, embedded_lines)
    return gmsh


# --------------------------------------------------------------------------- #
# anisotropic, flow-aligned size field (channel/floodplain zones + centerline)
# --------------------------------------------------------------------------- #


def _anisotropic_enabled(cfg: Config) -> bool:
    return (cfg.geodata.mesh_zones is not None
            and cfg.geodata.channel_centerline is not None)


_DEFAULT_BG_BUDGET = 40000
# BAMG point-insertion can fail ("Fatal error in the meshgenerator 1001 / BAMG
# failed") at fine, high-contrast anisotropic metrics: as the convergence study
# refines (small refinement zone next to a coarser floodplain), the metric field
# gets too steep for BAMG's Delaunay insertion to satisfy. Empirically a *coarser*
# background metric grid rescues it (its larger triangles smooth the interpolated
# metric, so BAMG is asked to honour a gentler gradient), as does a looser
# anisotropy cap. Retry ladder of (background-grid node budget, anisotropy-cap
# relaxation), each attempt gentler than the last, before giving up.
_BAMG_RETRY_LADDER = [(_DEFAULT_BG_BUDGET, 1.0), (20000, 1.0), (10000, 1.5),
                      (6000, 2.5)]


def _is_bamg_failure(exc: BaseException) -> bool:
    return "bamg" in str(exc).lower()


# These helpers moved to hydromate.core.geodata when the fourteen scattered layer
# readers were unified behind one cached Dataset. They stay reachable from here
# because existing code and tests import hydromate.mesh._parse_decimal and friends.
_ZONE_PRIORITY = geodata.ZONE_PRIORITY
_match_field = geodata.match_field
_parse_decimal = geodata.parse_decimal
_classify_zone = geodata.classify_zone
_fill_holes = geodata.fill_holes


def _read_mesh_zones(cfg: Config):
    """Mesh-zone polygons with the derived ``_zone_type`` / ``_edge_length`` /
    ``_prio`` columns. See :meth:`hydromate.core.geodata.Dataset.mesh_zones`."""
    return dataset(cfg).mesh_zones()


def _channel_union(cfg: Config):
    """Channel footprint: union of the '*channel*' mesh zones, holes filled.
    See :meth:`hydromate.core.geodata.Dataset.channel_union`."""
    return dataset(cfg).channel_union()


def nominal_channel_size(cfg: Config) -> float:
    """Effective (scaled) target channel edge length [m].
    See :func:`hydromate.core.geodata.nominal_channel_size` - it moved to the core
    because it reads the shared mesh zones, and both meshers size themselves by it."""
    return geodata.nominal_channel_size(cfg)



def _centerline_tangents(cfg: Config, spacing: float):
    """Sample the channel centerline -> (points, unit tangents, KD-tree)."""
    return dataset(cfg).centerline_tangents(spacing)


def _assign_point_zones(cfg: Config, zones, pts: np.ndarray):
    """For each background grid point return (zone type, target edge length).

    Points are spatially joined to the mesh-zone polygons; where a point falls in
    several zones, the highest-priority one (``_prio``: refinement > channel >
    floodplain) wins. Points outside every zone get the configured
    ``floodplain_size`` (the background/default size).
    """
    import geopandas as gpd

    pgdf = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(pts[:, 0], pts[:, 1]), crs=f"EPSG:{cfg.crs_epsg}")
    j = gpd.sjoin(pgdf, zones[["geometry", "_zone_type", "_edge_length", "_prio"]],
                  how="left", predicate="within")
    # one point may match several polygons -> keep the finest-intent (lowest _prio)
    j = (j.sort_values("_prio").reset_index()
         .drop_duplicates("index", keep="first").set_index("index").sort_index())
    ztype = j["_zone_type"].fillna("other").to_numpy()
    edge = (j["_edge_length"]
            .fillna(cfg.mesh.floodplain_size * cfg.mesh.size_scale)
            .to_numpy(dtype=float))
    return ztype, edge


def _metric_view(cfg: Config, poly, *, bg_budget: int = 40000):
    """Build the metric-tensor background mesh as a gmsh list view.

    The metric M at a node encodes the target edge lengths (a unit edge in
    direction d satisfies d·M·d = 1). Each background node is sized from the
    mesh-zone polygon it falls in (:func:`_read_mesh_zones`): ``channel`` zones are
    anisotropic (long axis along the centerline tangent, ``edge * channel_anisotropy``;
    short axis ``edge`` across); ``floodplain`` and ``refinement`` zones are
    isotropic at their ``edge`` length (refinement zones are simply a finer
    isotropic ``edge`` for local refinement); points outside every zone get the
    default ``floodplain_size``. A coarse background grid suffices - gmsh
    interpolates the metric within each background triangle, yielding the smooth
    blend between zones.
    """
    import gmsh
    from scipy.spatial import Delaunay

    m = cfg.mesh
    zones = _read_mesh_zones(cfg)
    minx, miny, maxx, maxy = poly.bounds
    area = (maxx - minx) * (maxy - miny)
    # coarse background grid, node budget capped so any domain stays in memory
    fp = m.floodplain_size * m.size_scale
    step = max(fp, math.sqrt(area / float(bg_budget)))
    cpts, tang, tree = _centerline_tangents(cfg, spacing=max(step, fp))

    gx, gy = np.meshgrid(np.arange(minx, maxx + step, step),
                         np.arange(miny, maxy + step, step))
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    simplices = Delaunay(pts).simplices

    ztype, edge = _assign_point_zones(cfg, zones, pts)

    # isotropic metric everywhere (1/edge^2 on the diagonal), then override channel
    inv = 1.0 / edge**2
    metrics = np.zeros((len(pts), 9))
    metrics[:, 0] = metrics[:, 4] = inv
    metrics[:, 8] = 1.0
    ch = np.flatnonzero(ztype == "channel")
    if ch.size:
        t = tang[tree.query(pts[ch])[1]]              # local centerline tangents
        nrm = np.column_stack([-t[:, 1], t[:, 0]])    # cross-channel normals
        inv_cross = 1.0 / edge[ch] ** 2               # across: the zone edge length
        inv_along = 1.0 / (edge[ch] * m.channel_anisotropy) ** 2   # along: stretched
        metrics[ch, 0] = inv_along * t[:, 0] ** 2 + inv_cross * nrm[:, 0] ** 2
        metrics[ch, 1] = inv_along * t[:, 0] * t[:, 1] + inv_cross * nrm[:, 0] * nrm[:, 1]
        metrics[ch, 3] = metrics[ch, 1]
        metrics[ch, 4] = inv_along * t[:, 1] ** 2 + inv_cross * nrm[:, 1] ** 2

    P = pts[simplices]
    data = np.concatenate(
        [P[:, :, 0], P[:, :, 1], np.zeros((len(simplices), 3)),
         metrics[simplices].reshape(len(simplices), -1)], axis=1
    ).ravel()
    view = gmsh.view.add("mesh-metric")
    gmsh.view.addListData(view, "TT", len(simplices), data.tolist())
    return view


def _anisotropic_size_field(cfg: Config, poly, *, bg_budget: int = 40000,
                            aniso_relax: float = 1.0) -> None:
    import gmsh

    m = cfg.mesh
    view = _metric_view(cfg, poly, bg_budget=bg_budget)
    bg = gmsh.model.mesh.field.add("PostView")
    gmsh.model.mesh.field.setNumber(bg, "ViewIndex", 0)
    gmsh.model.mesh.field.setAsBackgroundMesh(bg)

    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
    gmsh.option.setNumber("Mesh.SmoothRatio", m.growth_ratio)
    # Cap the metric anisotropy. BAMG overshoots the metric in the tail by ~1.6x,
    # so target max_aspect_ratio/1.6 here; the post-mesh edge flips then trim the
    # remaining over-sharp cells so no triangle exceeds ~max_aspect_ratio:1.
    # *aniso_relax* (>1 on a retry) loosens the cap to coax BAMG past a point-
    # insertion failure at fine, high-contrast metrics (see build_mesh's retry).
    gmsh.option.setNumber("Mesh.AnisoMax", max(1.5, m.max_aspect_ratio / 1.6 * aniso_relax))
    gmsh.option.setNumber("Mesh.Algorithm", 7)   # BAMG (anisotropic 2D)
    _ = view


def _size_fields(cfg: Config, breakline_curves: list[int]):
    import gmsh

    m = cfg.mesh
    scale = float(m.size_scale or 1.0)
    default_size = m.default_size * scale
    breakline_size = m.breakline_size * scale
    field = gmsh.model.mesh.field
    thresholds: list[int] = []

    if breakline_curves:
        dist = field.add("Distance")
        field.setNumbers(dist, "CurvesList", breakline_curves)
        field.setNumber(dist, "Sampling", 100)
        thr = field.add("Threshold")
        field.setNumber(thr, "InField", dist)
        field.setNumber(thr, "SizeMin", breakline_size)
        field.setNumber(thr, "SizeMax", default_size)
        field.setNumber(thr, "DistMin", breakline_size)
        field.setNumber(thr, "DistMax", default_size * 3)
        thresholds.append(thr)

    # per-region refinement around MATID seed points
    xy, matids = _read_region_seeds(cfg)
    if xy is not None and m.region_sizes:
        for matid, size in m.region_sizes.items():
            size = size * scale
            sel = xy[matids == matid]
            if len(sel) == 0:
                continue
            pts = [gmsh.model.geo.addPoint(px, py, 0.0, size) for px, py in sel]
            gmsh.model.geo.synchronize()
            dist = field.add("Distance")
            field.setNumbers(dist, "PointsList", pts)
            thr = field.add("Threshold")
            field.setNumber(thr, "InField", dist)
            field.setNumber(thr, "SizeMin", size)
            field.setNumber(thr, "SizeMax", default_size)
            field.setNumber(thr, "DistMin", size)
            field.setNumber(thr, "DistMax", default_size * 5)
            thresholds.append(thr)

    if thresholds:
        bg = field.add("Min")
        field.setNumbers(bg, "FieldsList", thresholds)
        field.setAsBackgroundMesh(bg)

    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 1)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
    gmsh.option.setNumber("Mesh.MeshSizeMin", m.min_size * scale)
    gmsh.option.setNumber("Mesh.Algorithm", m.algorithm)


# --------------------------------------------------------------------------- #
# aspect-ratio limiting (post-mesh smoothing of over-sharp triangles)
# --------------------------------------------------------------------------- #


def _tri_aspect(x, y, tri):
    """Per-triangle (longest/shortest edge) aspect ratio and 2*signed area."""
    v0, v1, v2 = tri[:, 0], tri[:, 1], tri[:, 2]
    L0 = np.hypot(x[v1] - x[v2], y[v1] - y[v2])
    L1 = np.hypot(x[v2] - x[v0], y[v2] - y[v0])
    L2 = np.hypot(x[v0] - x[v1], y[v0] - y[v1])
    L = np.stack([L0, L1, L2], axis=1)
    lmin = L.min(axis=1)
    aspect = np.where(lmin > 0, L.max(axis=1) / np.where(lmin > 0, lmin, 1.0), np.inf)
    area2 = ((x[v1] - x[v0]) * (y[v2] - y[v0]) - (x[v2] - x[v0]) * (y[v1] - y[v0]))
    return aspect, area2


def _one_aspect(p, q, r, x, y):
    """Aspect ratio (longest/shortest edge) of the single triangle (p, q, r)."""
    l0 = math.hypot(x[q] - x[r], y[q] - y[r])
    l1 = math.hypot(x[r] - x[p], y[r] - y[p])
    l2 = math.hypot(x[p] - x[q], y[p] - y[q])
    lo = min(l0, l1, l2)
    return max(l0, l1, l2) / lo if lo > 0 else math.inf


def _ccw_tri(p, q, r, x, y):
    """Return the triangle (p, q, r) re-ordered counter-clockwise (positive area)."""
    area2 = (x[q] - x[p]) * (y[r] - y[p]) - (x[r] - x[p]) * (y[q] - y[p])
    return (p, q, r) if area2 > 0 else (p, r, q)


def _flip_sharp_edges(triangles, x, y, max_ratio, *, max_passes: int = 12):
    """Reduce over-sharp triangles by flipping the shared edge of a too-elongated
    cell pair (a topological repair for the slivers BAMG leaves in the channel).

    For each interior edge whose two triangles exceed *max_ratio* (longest:shortest
    edge), flip the diagonal when both resulting triangles are valid (no inversion)
    and the pair's worst aspect drops. Boundary edges are never touched, so IPOBO
    stays consistent; node coordinates are unchanged. Returns the new connectivity.
    """
    tri = np.array(triangles, dtype=np.int64, copy=True)
    n = tri.shape[0]
    for _ in range(max_passes):
        aspect = _tri_aspect(x, y, tri)[0]
        if aspect.max() <= max_ratio:
            break
        # interior edges -> their two triangles (vectorised)
        e = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
        e = np.sort(e, axis=1)
        tid = np.tile(np.arange(n), 3)
        order = np.lexsort((e[:, 1], e[:, 0]))
        e, tid = e[order], tid[order]
        dup = np.where(np.all(e[1:] == e[:-1], axis=1))[0]
        if dup.size == 0:
            break
        t1, t2 = tid[dup], tid[dup + 1]
        pair_max = np.maximum(aspect[t1], aspect[t2])
        sel = pair_max > max_ratio
        # worst pairs first so a flip is not pre-empted by a neighbour's flip
        cand = np.argsort(pair_max[sel])[::-1]
        dup_sel, t1_sel, t2_sel = dup[sel], t1[sel], t2[sel]
        dead = np.zeros(n, dtype=bool)
        n_flip = 0
        for c in cand:
            i1, i2 = int(t1_sel[c]), int(t2_sel[c])
            if dead[i1] or dead[i2]:
                continue
            a, b = int(e[dup_sel[c]][0]), int(e[dup_sel[c]][1])
            r = [v for v in tri[i1] if v != a and v != b]
            s = [v for v in tri[i2] if v != a and v != b]
            if len(r) != 1 or len(s) != 1:
                continue
            r, s = int(r[0]), int(s[0])
            # valid (convex-quad) flip iff a and b are on opposite sides of the new
            # diagonal r-s; otherwise the flipped triangles would overlap.
            dx, dy = x[s] - x[r], y[s] - y[r]
            cra = dx * (y[a] - y[r]) - dy * (x[a] - x[r])
            crb = dx * (y[b] - y[r]) - dy * (x[b] - x[r])
            if cra * crb >= 0.0:
                continue
            new1 = _ccw_tri(a, r, s, x, y)
            new2 = _ccw_tri(b, r, s, x, y)
            if max(_one_aspect(*new1, x, y), _one_aspect(*new2, x, y)) \
                    < max(aspect[i1], aspect[i2]):
                tri[i1] = new1
                tri[i2] = new2
                dead[i1] = dead[i2] = True
                n_flip += 1
        if n_flip == 0:
            break
    return tri


# --------------------------------------------------------------------------- #
# boundary contour -> IPOBO
# --------------------------------------------------------------------------- #


def _order_boundary(triangles: np.ndarray, npoin: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(boundary_nodes, loop_lengths)``.

    ``boundary_nodes`` are the boundary node indices ordered along the contour;
    ``loop_lengths`` gives the node count of each loop (in the same order they
    appear in ``boundary_nodes``), so callers can segment the flat array back into
    its individual closed loops.

    Boundary edges occur in exactly one triangle. We chain them into a loop. For
    a domain with islands there are several loops; we return the longest one
    (outer contour) first, then remaining loops appended - enough for IPOBO,
    which only needs a consistent global boundary numbering.
    """
    from collections import defaultdict

    edge_count: dict[tuple[int, int], int] = defaultdict(int)
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge_count[(min(a, b), max(a, b))] += 1
    boundary_edges = [e for e, c in edge_count.items() if c == 1]

    adj: dict[int, list[int]] = defaultdict(list)
    for a, b in boundary_edges:
        adj[a].append(b)
        adj[b].append(a)

    visited_edges: set[tuple[int, int]] = set()
    loops: list[list[int]] = []
    for start in list(adj):
        if all((min(start, n), max(start, n)) in visited_edges for n in adj[start]):
            continue
        loop = [start]
        prev, cur = None, start
        while True:
            nxts = [n for n in adj[cur]
                    if (min(cur, n), max(cur, n)) not in visited_edges]
            if not nxts:
                break
            nxt = nxts[0] if nxts[0] != prev or len(nxts) == 1 else nxts[1]
            visited_edges.add((min(cur, nxt), max(cur, nxt)))
            if nxt == start:
                break
            loop.append(nxt)
            prev, cur = cur, nxt
        loops.append(loop)

    loops.sort(key=len, reverse=True)
    flat = np.array([n for loop in loops for n in loop], dtype=int)
    loop_lengths = np.array([len(loop) for loop in loops], dtype=int)
    return flat, loop_lengths


# --------------------------------------------------------------------------- #
# bathymetry + MATID assignment
# --------------------------------------------------------------------------- #




def _assign_matid(cfg: Config, points: np.ndarray) -> np.ndarray:
    """MATID at arbitrary points: nearest region seed point; default 1 if none."""
    xy, matids = _read_region_seeds(cfg)
    if xy is None:
        return np.ones(len(points), dtype=int)
    from scipy.spatial import cKDTree

    _, idx = cKDTree(xy).query(points)
    return matids[idx]


def channel_node_mask(cfg: Config, mesh: "Mesh") -> np.ndarray:
    """Boolean (NPOIN,) flag of mesh nodes on/inside the ``*channel*`` mesh-zones.

    Uses the same ``geodata.mesh_zones`` polygons (``Zone Name`` contains
    ``channel``) that drive the anisotropic mesh, so the pre-wetting region
    coincides with the meshed channel. Raises if no channel zones are configured.

    The footprint is buffered by ~one cell before the point-in-polygon test so
    that boundary nodes lying *on* the channel's outer edge are included. The
    inflow/outflow liquid boundaries coincide with that edge, and strict
    containment would drop those nodes - leaving the prescribed-discharge inflow
    cross-section dry, which makes TELEMAC's ``DEBIMP`` abort at t=0 ("PROBLEM ON
    BOUNDARY NUMBER ... CHECK THE WATER DEPTHS"). The seeded depth is still
    ``max(water_level - bed, 0)``, so the buffer only wets nodes actually below
    the hotstart surface; the dry banks stay dry.
    """
    from shapely import contains_xy

    channel = _channel_union(cfg)
    tol = max(cfg.mesh.channel_size, cfg.mesh.floodplain_size) * cfg.mesh.size_scale
    return np.asarray(contains_xy(channel.buffer(tol), mesh.x, mesh.y), dtype=bool)


def _channel_centroid_mask(cfg: Config, centroids: np.ndarray) -> np.ndarray | None:
    """Boolean (NELEM,) flag of element centroids inside the channel zones.

    Used only to report the deliberately anisotropic channel separately from the
    floodplain in the quality assessment; returns None if no channel zones.
    """
    if not _anisotropic_enabled(cfg):
        return None
    try:
        from shapely import contains_xy

        channel = _channel_union(cfg)
        return np.asarray(contains_xy(channel, centroids[:, 0], centroids[:, 1]),
                          dtype=bool)
    except Exception as exc:  # pragma: no cover - reporting aid only, never fatal
        log.debug("channel mask for quality report unavailable: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #


def build_mesh(cfg: Config, dem_initial_roi: Path | None = None) -> Mesh:
    """Generate the mesh (geometry + MATID). When *dem_initial_roi* is given,
    its elevations are interpolated onto the nodes; otherwise the bottom is left
    at zero and can be filled later with :func:`interpolate_elevations`.
    """
    if _anisotropic_enabled(cfg):
        zones = _read_mesh_zones(cfg)
        summary = ", ".join(
            f"{zt} {grp['_edge_length'].min():.2f}-{grp['_edge_length'].max():.2f} m"
            f" (x{len(grp)})"
            for zt, grp in zones.groupby("_zone_type"))
        scale_note = (f", size scale x{cfg.mesh.size_scale:.3f}"
                      if cfg.mesh.size_scale != 1.0 else "")
        log.info("  mesh strategy: anisotropic (channel x%.1f along centerline, "
                 "growth %.2f%s); zones from %s: %s", cfg.mesh.channel_anisotropy,
                 cfg.mesh.growth_ratio, scale_note,
                 Path(cfg.geodata.mesh_zones).name, summary)
    else:
        log.info("  mesh strategy: isotropic (default %.2f m, breakline %.2f m)",
                 cfg.mesh.default_size * cfg.mesh.size_scale,
                 cfg.mesh.breakline_size * cfg.mesh.size_scale)
    # BAMG can hit a point-insertion failure at fine, high-contrast anisotropic
    # metrics; retry with a denser background metric grid + looser cap before
    # giving up (see _BAMG_RETRY_LADDER). Isotropic meshing has a single attempt.
    anisotropic = _anisotropic_enabled(cfg)
    attempts = _BAMG_RETRY_LADDER if anisotropic else [(_DEFAULT_BG_BUDGET, 1.0)]
    x = y = triangles = None
    for attempt, (bg_budget, aniso_relax) in enumerate(attempts):
        gmsh = _build_gmsh(cfg, bg_budget=bg_budget, aniso_relax=aniso_relax)
        step_name = ("  gmsh triangulation" if attempt == 0
                     else f"  gmsh triangulation (BAMG retry {attempt}: background "
                          f"{bg_budget // 1000}k nodes, anisotropy x{aniso_relax:g})")
        try:
            with log_step(step_name):
                gmsh.model.mesh.generate(2)
            node_tags, node_coords, _ = gmsh.model.mesh.getNodes()
            node_coords = np.array(node_coords).reshape(-1, 3)
            all_tag2idx = {int(t): i for i, t in enumerate(node_tags)}

            etypes, _, enodes = gmsh.model.mesh.getElements(dim=2)
            tris = None
            for et, en in zip(etypes, enodes):
                if et == 2:  # 3-node triangle
                    tris = np.array(en, dtype=np.int64).reshape(-1, 3)
            if tris is None:
                raise RuntimeError("gmsh produced no triangles")
            # keep only nodes referenced by triangles (gmsh also returns geometry /
            # 1D-curve nodes); remap those to dense 0-based indices so the geometry
            # carries no orphan or duplicate nodes.
            used_tags = np.unique(tris)
            tag2idx = {int(t): i for i, t in enumerate(used_tags)}
            src = np.fromiter((all_tag2idx[int(t)] for t in used_tags), dtype=np.int64,
                              count=used_tags.size)
            x = node_coords[src, 0].copy()
            y = node_coords[src, 1].copy()
            triangles = np.vectorize(tag2idx.get)(tris)
            break
        except Exception as exc:
            if anisotropic and _is_bamg_failure(exc) and attempt < len(attempts) - 1:
                nb, nr = attempts[attempt + 1]
                log.warning("  BAMG meshing failed (%s); retrying with a coarser "
                            "background metric grid (%d nodes) and a relaxed "
                            "anisotropy cap (x%.1f) for a gentler metric", exc, nb, nr)
                continue
            raise
        finally:
            gmsh.finalize()

    npoin = x.size
    # enforce consistent counter-clockwise winding (no inverted elements)
    signed2 = ((x[triangles[:, 1]] - x[triangles[:, 0]])
               * (y[triangles[:, 2]] - y[triangles[:, 0]])
               - (x[triangles[:, 2]] - x[triangles[:, 0]])
               * (y[triangles[:, 1]] - y[triangles[:, 0]]))
    flip = signed2 < 0
    if flip.any():
        triangles[flip] = triangles[flip][:, [0, 2, 1]]
        log.debug("flipped %d triangle(s) to consistent CCW orientation", int(flip.sum()))

    # cap the cell aspect ratio (shortest:longest edge): flip the shared edge of
    # the over-sharp cell pairs BAMG leaves in the channel (topological repair;
    # boundary untouched, bulk channel anisotropy preserved).
    if cfg.mesh.max_aspect_ratio and cfg.mesh.max_aspect_ratio > 1.0:
        before = float(_tri_aspect(x, y, triangles)[0].max())
        with log_step("  limit cell aspect ratio (edge flips)"):
            triangles = _flip_sharp_edges(triangles, x, y, cfg.mesh.max_aspect_ratio)
        after = float(_tri_aspect(x, y, triangles)[0].max())
        log.info("  aspect-ratio cap %.1f:1 (worst %.2f:1 -> %.2f:1 after flips)",
                 cfg.mesh.max_aspect_ratio, before, after)

    boundary_nodes, boundary_loops = _order_boundary(triangles, npoin)
    ipobo = np.zeros(npoin, dtype=int)
    ipobo[boundary_nodes] = np.arange(1, boundary_nodes.size + 1)

    bottom = (sample_raster_at(Path(dem_initial_roi), x, y)
              if dem_initial_roi is not None else np.zeros(npoin))
    if dem_initial_roi is not None:
        # dams, weirs, walls and buildings are terrain the DEM did not carry
        bottom = _burn_structures(cfg, Mesh(
            x=x, y=y, triangles=triangles, bottom=bottom, ipobo=ipobo,
            boundary_nodes=boundary_nodes,
            element_matid=np.ones(len(triangles), dtype=int),
            node_matid=np.ones(npoin, dtype=int)), bottom)
    centroids = np.column_stack([
        x[triangles].mean(axis=1), y[triangles].mean(axis=1)
    ])
    element_matid = _assign_matid(cfg, centroids)
    node_matid = _assign_matid(cfg, np.column_stack([x, y]))

    mesh = Mesh(
        x=x, y=y, triangles=triangles, bottom=bottom, ipobo=ipobo,
        boundary_nodes=boundary_nodes, element_matid=element_matid,
        node_matid=node_matid, boundary_loops=boundary_loops,
    )

    # quality assessment + validity gate (channel reported separately, its
    # anisotropy is intended; fatal geometry defects abort the build)
    from hydromate.solvers.telemac import mesh_quality

    with log_step("  mesh-quality assessment"):
        mesh.quality = mesh_quality.assess_quality(
            mesh, channel_mask=_channel_centroid_mask(cfg, centroids),
            min_angle_deg=cfg.mesh.min_angle_deg, max_angle_deg=cfg.mesh.max_angle_deg,
            area_jump_threshold=cfg.mesh.max_area_jump,
        )
    mesh_quality.log_report(mesh.quality, log)
    if mesh.quality.is_fatal:
        raise ValueError(
            f"invalid mesh geometry: {mesh.quality.n_zero_area} zero-area, "
            f"{mesh.quality.n_inverted} inverted, {mesh.quality.n_duplicate_nodes} "
            f"duplicate-node, {mesh.quality.n_nonmanifold_edges} non-manifold-edge "
            "defect(s). Adjust the mesh sizes/boundary and rebuild."
        )
    return mesh


def interpolate_elevations(mesh: Mesh, dem_path: Path, *, decimals: int = 4,
                           cfg: Config | None = None) -> Mesh:
    """Interpolate DEM elevations onto the mesh nodes (in place) and return it.

    Elevations are rounded to *decimals* digits after the decimal point (4 by
    default). NaNs (nodata / just outside the grid) are nearest-neighbour filled.

    With *cfg*, the case's structures (:mod:`hydromate.core.structures`) are burnt
    into the bed afterwards. A **depth-averaged model has no vertical wall to remove**,
    so both structure modes end up as terrain here: an ``overflow`` structure raises
    the bed to its crest, and a ``solid`` one raises it to the crest plus
    ``structures.solid_freeboard_2d`` so it cannot be overtopped. That is the standard
    way a floodwall is represented in TELEMAC, and it is the one place where the two
    solvers necessarily differ - OpenFOAM removes the footprint and gets a real
    vertical face, which is reported in the build notes rather than left implicit.
    """
    z = sample_raster_at(Path(dem_path), mesh.x, mesh.y)
    if cfg is not None:
        z = _burn_structures(cfg, mesh, z)
    mesh.bottom = np.round(z, decimals) if decimals is not None else z
    return mesh


def _burn_structures(cfg: Config, mesh: Mesh, z: np.ndarray) -> np.ndarray:
    """Raise the bed for this case's structures (see :func:`interpolate_elevations`)."""
    from hydromate.core.structures import (
        OVERFLOW, SOLID, Structure, apply_to_bed, load_structures,
    )

    structures = load_structures(cfg)
    if not structures:
        return z
    xy = np.column_stack([mesh.x, mesh.y])
    # a 2D mesh cannot delete a footprint, so a solid structure becomes an
    # un-overtoppable ridge: crest + freeboard, as terrain
    freeboard = float(cfg.structures.solid_freeboard_2d)
    as_terrain = [
        s if s.mode == OVERFLOW else Structure(
            name=f"{s.name} (solid -> terrain +{freeboard:g} m)", mode=OVERFLOW,
            polygon=s.polygon,
            crest=None if s.crest is None else s.crest + freeboard,
            height=None if s.height is None else s.height + freeboard)
        for s in structures
    ]
    if any(s.mode == SOLID for s in structures):
        log.info("  solid structures raised to crest + %.2f m freeboard: a 2D mesh "
                 "has no vertical wall to remove", freeboard)
    z, _ = apply_to_bed(as_terrain, xy, z)
    return z


def read_roughness_table(path: Path) -> dict[int, float]:
    """Read the zone-roughness CSV -> ``{zone_id: roughness}``.
    See :func:`hydromate.core.geodata.read_roughness_table`."""
    return geodata.read_roughness_table(path)


def interpolate_roughness(cfg: Config, mesh: Mesh) -> Mesh:
    """Map the roughness zones onto the mesh: friction zone id + roughness value.

    Each node (and element, by its centroid) is tagged with the
    ``roughness_zone_field`` id of the polygon it falls in (nearest polygon for
    points just outside any). That id becomes the per-node ``FRIC_ID`` written to
    the geometry - overriding the MATID-derived ids so the friction zonation comes
    from the roughness zones - via ``mesh.node_matid`` / ``mesh.element_matid``.
    The matching ``geodata.roughness_table`` value (e.g. a Nikuradse k_s) is stored
    in ``mesh.roughness`` (BOTTOM FRICTION); HydroBayesCal later perturbs it.
    """
    import geopandas as gpd

    if cfg.geodata.roughness_zones is None or cfg.geodata.roughness_table is None:
        raise ValueError("interpolate_roughness needs geodata.roughness_zones "
                         "and geodata.roughness_table to be set")
    table = read_roughness_table(Path(cfg.geodata.roughness_table))

    zones = dataset(cfg).roughness_zones()
    if zones.crs and zones.crs.to_epsg() != cfg.crs_epsg:
        zones = zones.to_crs(epsg=cfg.crs_epsg)
    field = next((c for c in zones.columns
                  if c.lower() == cfg.mesh.roughness_zone_field.lower()), None)
    if field is None:
        raise ValueError(
            f"roughness_zones {Path(cfg.geodata.roughness_zones).name!r} has no "
            f"'{cfg.mesh.roughness_zone_field}' column (has {list(zones.columns)})"
        )
    zones = zones[[field, "geometry"]].rename(columns={field: "zone_id"})

    def _zone_of(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        pts = gpd.GeoDataFrame(
            geometry=gpd.points_from_xy(xs, ys), crs=f"EPSG:{cfg.crs_epsg}"
        )
        joined = gpd.sjoin_nearest(pts, zones, how="left").sort_index()
        # a point on a shared edge can match >1 polygon; keep the first per point
        ids = joined[~joined.index.duplicated()]["zone_id"].to_numpy()
        return ids.astype(int)

    node_zone = _zone_of(mesh.x, mesh.y)
    cx = mesh.x[mesh.triangles].mean(axis=1)
    cy = mesh.y[mesh.triangles].mean(axis=1)
    elem_zone = _zone_of(cx, cy)

    seen_zones = set(node_zone.tolist()) | set(elem_zone.tolist())
    missing = sorted(seen_zones - set(table))
    if missing:
        raise ValueError(
            f"roughness zone id(s) {missing} present in "
            f"{Path(cfg.geodata.roughness_zones).name!r} but absent from the "
            f"roughness table {Path(cfg.geodata.roughness_table).name!r} "
            f"(has {sorted(table)})"
        )
    mesh.node_matid = node_zone                       # FRIC_ID per node
    mesh.element_matid = elem_zone                    # MATID per element
    mesh.roughness = np.array([table[z] for z in node_zone], dtype=float)
    counts = {int(z): int((node_zone == z).sum()) for z in sorted(set(node_zone.tolist()))}
    log.info("  roughness zones -> FRIC_ID / BOTTOM FRICTION: "
             "%s (zone:ks=%s)", counts, {z: table[z] for z in counts})
    return mesh


def write_mesh(mesh: Mesh, path: Path, *, title: str = "mesh") -> Path:
    """Write a :class:`Mesh` to a TELEMAC geometry SELAFIN file.

    Includes the per-node roughness as ``BOTTOM FRICTION`` when it has been set
    (see :func:`interpolate_roughness`).
    """
    path = Path(path)
    selafin.write_geometry(
        path,
        x=mesh.x, y=mesh.y,
        ikle=mesh.triangles + 1,                # SELAFIN is 1-based
        ipobo=mesh.ipobo, bottom=mesh.bottom,
        friction_id=mesh.node_matid,
        roughness=mesh.roughness,
        title=title,
    )
    return path


def run(cfg: Config, dem_initial_roi: Path) -> tuple[Mesh, Path]:
    """Build the mesh and write the geometry SELAFIN; returns (mesh, slf path).

    When roughness zones are configured, their ``Zone ID`` drives the per-node
    ``FRIC_ID`` and the table ks is written as ``BOTTOM FRICTION`` (so the geometry
    is consistent with the friction ``.tbl`` derived from the same table).
    """
    mesh = build_mesh(cfg, dem_initial_roi)
    if cfg.geodata.roughness_zones is not None and cfg.geodata.roughness_table is not None:
        with log_step("  interpolate roughness zones onto the mesh"):
            mesh = interpolate_roughness(cfg, mesh)
    slf_path = write_mesh(mesh, cfg.model_path(cfg.geometry_slf),
                          title=f"{cfg.name} geometry")
    return mesh, slf_path


# Public since the OpenFOAM extension samples the same DEM and the same ROI onto its
# own lattice; the underscore spellings stay as aliases for existing callers.
_boundary_polygon = roi_polygon
# sample_raster_at moved to hydromate.core.raster (both meshers need it and
# neither owns the DEM); both historic spellings still resolve from here.
_interpolate_bottom = sample_raster_at
