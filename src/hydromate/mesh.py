"""Stage 2 - mesh generation, bathymetry, and SELAFIN geometry.

Builds a triangular mesh with gmsh from the ROI boundary polygon and the
breaklines, with per-region (MATID) size refinement, then:

* extracts nodes + triangles,
* walks the mesh boundary to build the ordered contour (drives both TELEMAC's
  IPOBO array and the ``.cli`` row order - they must agree),
* interpolates the clipped DEM onto the nodes (bathymetry),
* assigns each element a MATID (friction zone),
* writes the geometry ``.slf`` via :mod:`hydromate.selafin`.

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
from hydromate import selafin
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


def _boundary_polygon(cfg: Config):
    """Return the ROI as a shapely Polygon (polygonising lines if needed)."""
    import geopandas as gpd
    from shapely.ops import polygonize, unary_union

    gdf = gpd.read_file(cfg.inputs.boundary)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    geoms = list(gdf.geometry.values)
    if set(gdf.geom_type) & {"LineString", "MultiLineString"}:
        polys = list(polygonize(unary_union(geoms)))
        if not polys:
            raise ValueError("boundary lines do not close into a polygon")
        return max(polys, key=lambda p: p.area)
    return max(geoms, key=lambda p: p.area)


def _read_lines(path: Path, crs_epsg: int):
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
    if cfg.inputs.region_points is None:
        return None, None
    import geopandas as gpd

    gdf = gpd.read_file(cfg.inputs.region_points)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    matid_col = next(
        (c for c in gdf.columns if c.upper() in ("MATID", "FRIC_ID", "MAT_ID")), None
    )
    xy = np.array([[g.x, g.y] for g in gdf.geometry.values])
    matids = (gdf[matid_col].astype(int).to_numpy() if matid_col
              else np.ones(len(gdf), dtype=int))
    return xy, matids


def _build_gmsh(cfg: Config):
    import gmsh

    m = cfg.mesh
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add(cfg.name)
    geo = gmsh.model.geo

    poly = _boundary_polygon(cfg)

    def add_ring(coords):
        pts = list(coords)
        if pts[0] == pts[-1]:
            pts = pts[:-1]
        tags = [geo.addPoint(px, py, 0.0, m.default_size) for px, py in pts]
        lines = [geo.addLine(tags[i], tags[(i + 1) % len(tags)]) for i in range(len(tags))]
        return geo.addCurveLoop(lines)

    outer = add_ring(poly.exterior.coords)
    holes = [add_ring(r.coords) for r in poly.interiors]
    surface = geo.addPlaneSurface([outer, *holes])

    # breaklines as embedded constraint lines
    embedded_lines: list[int] = []
    if cfg.inputs.breaklines is not None:
        for coords in _read_lines(Path(cfg.inputs.breaklines), cfg.crs_epsg):
            pts = [geo.addPoint(px, py, 0.0, m.breakline_size) for px, py in coords]
            for i in range(len(pts) - 1):
                embedded_lines.append(geo.addLine(pts[i], pts[i + 1]))

    geo.synchronize()
    if embedded_lines:
        gmsh.model.mesh.embed(1, embedded_lines, 2, surface)

    if _anisotropic_enabled(cfg):
        _anisotropic_size_field(cfg, poly)
    else:
        _size_fields(cfg, embedded_lines)
    return gmsh


# --------------------------------------------------------------------------- #
# anisotropic, flow-aligned size field (channel/floodplain zones + centerline)
# --------------------------------------------------------------------------- #


def _anisotropic_enabled(cfg: Config) -> bool:
    return (cfg.inputs.mesh_zones is not None
            and cfg.inputs.channel_centerline is not None)


_ZONE_PRIORITY = {"refinement": 0, "channel": 1, "floodplain": 2, "other": 3}


def _match_field(gdf, name: str) -> str | None:
    """Case-insensitive column lookup (so 'Zone Name'/'zone name' both match)."""
    return next((c for c in gdf.columns if str(c).lower() == name.lower()), None)


def _parse_decimal(value) -> float | None:
    """Parse a numeric mesh-zone field robustly.

    A GeoPackage ``double`` field already comes back as a float, but a field
    authored in a German-locale GIS may arrive as the *string* ``"0,5"``. Accept
    both: floats/ints pass through; strings have their decimal comma normalised to
    a point before parsing. Returns ``None`` for blanks / NaN / unparseable values
    so the caller can fall back to the configured default.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    s = str(value).strip().replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _classify_zone(name: str) -> str:
    """Map a 'Zone Name' to a zone type by substring (case-insensitive)."""
    n = str(name).lower()
    if "channel" in n:
        return "channel"
    if "refinement" in n:        # local refinement zones (isotropic, fine)
        return "refinement"
    if "floodplain" in n:
        return "floodplain"
    return "other"


def _read_mesh_zones(cfg: Config):
    """Read the mesh-zone polygons into a GeoDataFrame with, per polygon:

    * ``_zone_type`` - ``channel`` / ``floodplain`` / ``refinement`` / ``other``,
      from the ``Zone Name`` field (:func:`_classify_zone`),
    * ``_edge_length`` - the target max edge length [m], read from the
      ``Max Edge Length (m)`` field (``mesh.zone_size_field``) and parsed with
      :func:`_parse_decimal`; if that field is absent or blank for a polygon, the
      configured per-type default (``channel_size`` / ``floodplain_size`` /
      ``refinement_size``) is used,
    * ``_prio`` - overlap priority (refinement > channel > floodplain), so a point
      inside several zones takes the finest-intent zone.

    Reprojected to the project CRS.
    """
    import geopandas as gpd

    gdf = gpd.read_file(cfg.inputs.mesh_zones)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    name_field = _match_field(gdf, cfg.mesh.zone_name_field)
    if name_field is None:
        raise ValueError(
            f"mesh_zones {Path(cfg.inputs.mesh_zones).name!r} has no "
            f"'{cfg.mesh.zone_name_field}' column (has {list(gdf.columns)})"
        )
    size_field = _match_field(gdf, cfg.mesh.zone_size_field)
    defaults = {"channel": cfg.mesh.channel_size, "floodplain": cfg.mesh.floodplain_size,
                "refinement": cfg.mesh.refinement_size, "other": cfg.mesh.floodplain_size}

    ztypes = [_classify_zone(v) for v in gdf[name_field]]
    edge = []
    for zt, raw in zip(ztypes, (gdf[size_field] if size_field else [None] * len(gdf))):
        val = _parse_decimal(raw)
        edge.append(val if (val is not None and val > 0.0) else defaults[zt])
    return gdf.assign(_zone_type=ztypes, _edge_length=edge,
                      _prio=[_ZONE_PRIORITY[zt] for zt in ztypes])


def _fill_holes(geom):
    """Return *geom* with all interior rings removed (only the exterior kept).

    A mesh-zone author commonly carves holes into the channel polygon so a finer
    ``refinement`` zone can be nested inside (e.g. around a structure). Such a hole
    is enclosed by channel on every side, so it *is* channel - just meshed finer.
    Dropping the holes keeps those nested pockets part of the channel footprint so
    they are pre-wetted with the rest of the channel (otherwise the refinement
    zones start dry and look walled off; see :func:`channel_node_mask`).
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return unary_union([Polygon(p.exterior) for p in geom.geoms])
    return geom


def _channel_union(cfg: Config):
    """Channel footprint: union of the '*channel*' mesh zones, holes filled.

    Holes are filled (:func:`_fill_holes`) so refinement zones nested inside the
    channel polygon count as channel for pre-wetting and quality reporting.
    """
    from shapely.ops import unary_union

    zones = _read_mesh_zones(cfg)
    channel = zones[zones["_zone_type"] == "channel"]
    if channel.empty:
        raise ValueError(
            f"no mesh zone named '*channel*' in {Path(cfg.inputs.mesh_zones).name!r}; "
            f"found {sorted(zones['_zone_type'].unique())}"
        )
    return _fill_holes(unary_union(channel.geometry.values))


def _centerline_tangents(cfg: Config, spacing: float):
    """Sample the channel centerline -> (points, unit tangents, KD-tree)."""
    import geopandas as gpd
    from scipy.spatial import cKDTree
    from shapely.ops import linemerge, unary_union

    gdf = gpd.read_file(cfg.inputs.channel_centerline)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    merged = unary_union(gdf.geometry.values)
    line = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
    if line.geom_type == "MultiLineString":      # disjoint parts: take the longest
        line = max(line.geoms, key=lambda g: g.length)
    n = max(2, int(line.length / spacing))
    s = np.linspace(0.0, line.length, n)
    pts = np.array([[line.interpolate(d).x, line.interpolate(d).y] for d in s])
    tang = np.gradient(pts, axis=0)
    tang /= np.linalg.norm(tang, axis=1, keepdims=True).clip(1e-9)
    return pts, tang, cKDTree(pts)


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
    edge = j["_edge_length"].fillna(cfg.mesh.floodplain_size).to_numpy(dtype=float)
    return ztype, edge


def _metric_view(cfg: Config, poly):
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
    step = max(m.floodplain_size, math.sqrt(area / 40000.0))
    cpts, tang, tree = _centerline_tangents(cfg, spacing=max(step, m.floodplain_size))

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


def _anisotropic_size_field(cfg: Config, poly) -> None:
    import gmsh

    m = cfg.mesh
    view = _metric_view(cfg, poly)
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
    gmsh.option.setNumber("Mesh.AnisoMax", max(1.5, m.max_aspect_ratio / 1.6))
    gmsh.option.setNumber("Mesh.Algorithm", 7)   # BAMG (anisotropic 2D)
    _ = view


def _size_fields(cfg: Config, breakline_curves: list[int]):
    import gmsh

    m = cfg.mesh
    field = gmsh.model.mesh.field
    thresholds: list[int] = []

    if breakline_curves:
        dist = field.add("Distance")
        field.setNumbers(dist, "CurvesList", breakline_curves)
        field.setNumber(dist, "Sampling", 100)
        thr = field.add("Threshold")
        field.setNumber(thr, "InField", dist)
        field.setNumber(thr, "SizeMin", m.breakline_size)
        field.setNumber(thr, "SizeMax", m.default_size)
        field.setNumber(thr, "DistMin", m.breakline_size)
        field.setNumber(thr, "DistMax", m.default_size * 3)
        thresholds.append(thr)

    # per-region refinement around MATID seed points
    xy, matids = _read_region_seeds(cfg)
    if xy is not None and m.region_sizes:
        for matid, size in m.region_sizes.items():
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
            field.setNumber(thr, "SizeMax", m.default_size)
            field.setNumber(thr, "DistMin", size)
            field.setNumber(thr, "DistMax", m.default_size * 5)
            thresholds.append(thr)

    if thresholds:
        bg = field.add("Min")
        field.setNumbers(bg, "FieldsList", thresholds)
        field.setAsBackgroundMesh(bg)

    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 1)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
    gmsh.option.setNumber("Mesh.MeshSizeMin", m.min_size)
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


def _interpolate_bottom(dem_path: Path, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    import rasterio
    from scipy.interpolate import RegularGridInterpolator

    with rasterio.open(dem_path) as src:
        band = src.read(1, masked=True).astype(float)
        nodata = src.nodata
        t = src.transform
        rows = np.arange(src.height)
        cols = np.arange(src.width)
        xs = t.c + t.a * (cols + 0.5)
        ys = t.f + t.e * (rows + 0.5)

    data = np.array(band.filled(np.nan))
    if t.e < 0:  # north-up raster: flip rows so y is ascending for the interpolator
        ys = ys[::-1]
        data = data[::-1, :]

    interp = RegularGridInterpolator(
        (ys, xs), data, bounds_error=False, fill_value=np.nan
    )
    z = interp(np.column_stack([y, x]))

    # fill any NaNs (nodata / just outside grid) from nearest valid node
    nan = np.isnan(z)
    if nan.any():
        from scipy.spatial import cKDTree

        good = ~nan
        if not good.any():
            raise ValueError(f"DEM {dem_path} yielded no valid elevations on the mesh")
        tree = cKDTree(np.column_stack([x[good], y[good]]))
        _, idx = tree.query(np.column_stack([x[nan], y[nan]]))
        z[nan] = z[good][idx]
    if nodata is not None:
        z[z == nodata] = np.nan
    return z


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

    Uses the same ``inputs.mesh_zones`` polygons (``Zone Name`` contains
    ``channel``) that drive the anisotropic mesh, so the pre-wetting region
    coincides with the meshed channel. Raises if no channel zones are configured.

    The footprint is buffered by ~one cell before the point-in-polygon test so
    that boundary nodes lying *on* the channel's outer edge are included. The
    inflow/outflow liquid boundaries coincide with that edge, and strict
    containment would drop those nodes - leaving the prescribed-discharge inflow
    cross-section dry, which makes TELEMAC's ``DEBIMP`` abort at t=0 ("PROBLEM ON
    BOUNDARY NUMBER ... CHECK THE WATER DEPTHS"). The seeded depth is still
    ``max(water_level - bed, 0)``, so the buffer only wets nodes actually below
    the warm-start surface; the dry banks stay dry.
    """
    from shapely import contains_xy

    channel = _channel_union(cfg)
    tol = max(cfg.mesh.channel_size, cfg.mesh.floodplain_size)
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
        log.info("  mesh strategy: anisotropic (channel x%.1f along centerline, "
                 "growth %.2f); zones from %s: %s", cfg.mesh.channel_anisotropy,
                 cfg.mesh.growth_ratio, Path(cfg.inputs.mesh_zones).name, summary)
    else:
        log.info("  mesh strategy: isotropic (default %.2f m, breakline %.2f m)",
                 cfg.mesh.default_size, cfg.mesh.breakline_size)
    gmsh = _build_gmsh(cfg)
    try:
        with log_step("  gmsh triangulation"):
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

    bottom = (_interpolate_bottom(Path(dem_initial_roi), x, y)
              if dem_initial_roi is not None else np.zeros(npoin))
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
    from hydromate import mesh_quality

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


def interpolate_elevations(mesh: Mesh, dem_path: Path, *, decimals: int = 4) -> Mesh:
    """Interpolate DEM elevations onto the mesh nodes (in place) and return it.

    Elevations are rounded to *decimals* digits after the decimal point (4 by
    default). NaNs (nodata / just outside the grid) are nearest-neighbour filled.
    """
    z = _interpolate_bottom(Path(dem_path), mesh.x, mesh.y)
    mesh.bottom = np.round(z, decimals) if decimals is not None else z
    return mesh


def read_roughness_table(path: Path) -> dict[int, float]:
    """Read the zone-roughness CSV -> ``{zone_id: roughness}``.

    The first column is the integer zone id, the second the roughness value (e.g.
    a Nikuradse k_s) that HydroBayesCal later adjusts. A header row is detected
    and skipped; any further columns are ignored.
    """
    import pandas as pd

    df = pd.read_csv(Path(path), header=None)
    first = df.iloc[0]
    if pd.to_numeric(first.iloc[:2], errors="coerce").isna().any():
        df = df.iloc[1:]                         # the first row was a header
    ids = pd.to_numeric(df.iloc[:, 0], errors="coerce")
    vals = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    return {int(i): float(v) for i, v in zip(ids, vals) if not (np.isnan(i) or np.isnan(v))}


def interpolate_roughness(cfg: Config, mesh: Mesh) -> Mesh:
    """Map the roughness zones onto the mesh: friction zone id + roughness value.

    Each node (and element, by its centroid) is tagged with the
    ``roughness_zone_field`` id of the polygon it falls in (nearest polygon for
    points just outside any). That id becomes the per-node ``FRIC_ID`` written to
    the geometry - overriding the MATID-derived ids so the friction zonation comes
    from the roughness zones - via ``mesh.node_matid`` / ``mesh.element_matid``.
    The matching ``inputs.roughness_table`` value (e.g. a Nikuradse k_s) is stored
    in ``mesh.roughness`` (BOTTOM FRICTION); HydroBayesCal later perturbs it.
    """
    import geopandas as gpd

    if cfg.inputs.roughness_zones is None or cfg.inputs.roughness_table is None:
        raise ValueError("interpolate_roughness needs inputs.roughness_zones "
                         "and inputs.roughness_table to be set")
    table = read_roughness_table(Path(cfg.inputs.roughness_table))

    zones = gpd.read_file(cfg.inputs.roughness_zones)
    if zones.crs and zones.crs.to_epsg() != cfg.crs_epsg:
        zones = zones.to_crs(epsg=cfg.crs_epsg)
    field = next((c for c in zones.columns
                  if c.lower() == cfg.mesh.roughness_zone_field.lower()), None)
    if field is None:
        raise ValueError(
            f"roughness_zones {Path(cfg.inputs.roughness_zones).name!r} has no "
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
            f"{Path(cfg.inputs.roughness_zones).name!r} but absent from the "
            f"roughness table {Path(cfg.inputs.roughness_table).name!r} "
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
    if cfg.inputs.roughness_zones is not None and cfg.inputs.roughness_table is not None:
        with log_step("  interpolate roughness zones onto the mesh"):
            mesh = interpolate_roughness(cfg, mesh)
    slf_path = write_mesh(mesh, cfg.model_path(cfg.geometry_slf),
                          title=f"{cfg.name} geometry")
    return mesh, slf_path
