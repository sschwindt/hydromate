"""Stage 2 — mesh generation, bathymetry, and SELAFIN geometry.

Builds a triangular mesh with gmsh from the ROI boundary polygon and the
breaklines, with per-region (MATID) size refinement, then:

* extracts nodes + triangles,
* walks the mesh boundary to build the ordered contour (drives both TELEMAC's
  IPOBO array and the ``.cli`` row order — they must agree),
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
    roughness: np.ndarray | None = None  # (NPOIN,) per-node roughness value (e.g. ks)

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


def _channel_union(cfg: Config):
    """Union of the mesh-zone polygons whose name contains 'channel'."""
    import geopandas as gpd
    from shapely.ops import unary_union

    gdf = gpd.read_file(cfg.inputs.mesh_zones)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    field = next((c for c in gdf.columns
                  if c.lower() == cfg.mesh.zone_name_field.lower()), None)
    if field is None:
        raise ValueError(
            f"mesh_zones {Path(cfg.inputs.mesh_zones).name!r} has no "
            f"'{cfg.mesh.zone_name_field}' column (has {list(gdf.columns)})"
        )
    names = gdf[field].astype(str).str.lower()
    channel = gdf[names.str.contains("channel")]
    if channel.empty:
        raise ValueError(
            f"no mesh zone named '*channel*' in {Path(cfg.inputs.mesh_zones).name!r}; "
            f"found {sorted(gdf[field].astype(str).unique())}"
        )
    return unary_union(channel.geometry.values)


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


def _metric_view(cfg: Config, poly):
    """Build the metric-tensor background mesh as a gmsh list view.

    The metric M at a node encodes the target edge lengths (a unit edge in
    direction d satisfies d·M·d = 1): anisotropic inside the channel (long axis
    along the centerline tangent, ``channel_size * channel_anisotropy``; short
    axis ``channel_size`` across) and isotropic ``floodplain_size`` elsewhere.
    A coarse background grid suffices — gmsh interpolates the metric within each
    background triangle, which also yields the smooth channel->floodplain blend.
    """
    import gmsh
    from scipy.spatial import Delaunay
    from shapely.geometry import Point
    from shapely.prepared import prep

    m = cfg.mesh
    channel = prep(_channel_union(cfg))
    minx, miny, maxx, maxy = poly.bounds
    area = (maxx - minx) * (maxy - miny)
    # coarse background grid, node budget capped so any domain stays in memory
    step = max(m.floodplain_size, math.sqrt(area / 40000.0))
    cpts, tang, tree = _centerline_tangents(cfg, spacing=max(step, m.floodplain_size))

    gx, gy = np.meshgrid(np.arange(minx, maxx + step, step),
                         np.arange(miny, maxy + step, step))
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    simplices = Delaunay(pts).simplices

    h_along = m.channel_size * m.channel_anisotropy
    inv_along2, inv_cross2 = 1.0 / h_along**2, 1.0 / m.channel_size**2
    inv_fp2 = 1.0 / m.floodplain_size**2
    metrics = np.tile([inv_fp2, 0.0, 0.0, 0.0, inv_fp2, 0.0, 0.0, 0.0, 1.0],
                      (len(pts), 1))
    for k, p in enumerate(pts):
        if channel.contains(Point(p)):
            t = tang[tree.query(p)[1]]
            n = np.array([-t[1], t[0]])
            M = inv_along2 * np.outer(t, t) + inv_cross2 * np.outer(n, n)
            metrics[k] = [M[0, 0], M[0, 1], 0, M[1, 0], M[1, 1], 0, 0, 0, 1]

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
    gmsh.option.setNumber("Mesh.AnisoMax", max(2.0 * m.channel_anisotropy, 10.0))
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
# boundary contour -> IPOBO
# --------------------------------------------------------------------------- #


def _order_boundary(triangles: np.ndarray, npoin: int) -> np.ndarray:
    """Return boundary node indices ordered along the (single) outer contour.

    Boundary edges occur in exactly one triangle. We chain them into a loop. For
    a domain with islands there are several loops; we return the longest one
    (outer contour) first, then remaining loops appended — enough for IPOBO,
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
    return np.array([n for loop in loops for n in loop], dtype=int)


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


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #


def build_mesh(cfg: Config, dem_initial_roi: Path | None = None) -> Mesh:
    """Generate the mesh (geometry + MATID). When *dem_initial_roi* is given,
    its elevations are interpolated onto the nodes; otherwise the bottom is left
    at zero and can be filled later with :func:`interpolate_elevations`.
    """
    if _anisotropic_enabled(cfg):
        log.info("  mesh strategy: anisotropic (channel %.2f m x%.1f along centerline, "
                 "floodplain %.2f m, growth %.2f)", cfg.mesh.channel_size,
                 cfg.mesh.channel_anisotropy, cfg.mesh.floodplain_size,
                 cfg.mesh.growth_ratio)
    else:
        log.info("  mesh strategy: isotropic (default %.2f m, breakline %.2f m)",
                 cfg.mesh.default_size, cfg.mesh.breakline_size)
    gmsh = _build_gmsh(cfg)
    try:
        gmsh.model.mesh.generate(2)
        node_tags, node_coords, _ = gmsh.model.mesh.getNodes()
        node_coords = np.array(node_coords).reshape(-1, 3)
        # remap arbitrary gmsh tags to dense 0-based indices
        tag2idx = {int(t): i for i, t in enumerate(node_tags)}
        x = node_coords[:, 0].copy()
        y = node_coords[:, 1].copy()

        etypes, _, enodes = gmsh.model.mesh.getElements(dim=2)
        tris = None
        for et, en in zip(etypes, enodes):
            if et == 2:  # 3-node triangle
                tris = np.array(en, dtype=np.int64).reshape(-1, 3)
        if tris is None:
            raise RuntimeError("gmsh produced no triangles")
        triangles = np.vectorize(tag2idx.get)(tris)
    finally:
        gmsh.finalize()

    npoin = x.size
    boundary_nodes = _order_boundary(triangles, npoin)
    ipobo = np.zeros(npoin, dtype=int)
    ipobo[boundary_nodes] = np.arange(1, boundary_nodes.size + 1)

    bottom = (_interpolate_bottom(Path(dem_initial_roi), x, y)
              if dem_initial_roi is not None else np.zeros(npoin))
    centroids = np.column_stack([
        x[triangles].mean(axis=1), y[triangles].mean(axis=1)
    ])
    element_matid = _assign_matid(cfg, centroids)
    node_matid = _assign_matid(cfg, np.column_stack([x, y]))

    return Mesh(
        x=x, y=y, triangles=triangles, bottom=bottom, ipobo=ipobo,
        boundary_nodes=boundary_nodes, element_matid=element_matid,
        node_matid=node_matid,
    )


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
    """Assign each node a roughness value from the roughness zones + table.

    Each node is tagged with the ``roughness_zone_field`` id of the polygon it
    falls in (nearest polygon for nodes just outside any), then mapped to a
    roughness value via ``inputs.roughness_table``. Sets ``mesh.roughness`` and
    returns the mesh.
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

    nodes = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(mesh.x, mesh.y), crs=f"EPSG:{cfg.crs_epsg}"
    )
    joined = gpd.sjoin_nearest(nodes, zones, how="left").sort_index()
    # a node on a shared edge can match >1 polygon; keep the first per node
    zone_ids = joined[~joined.index.duplicated()]["zone_id"].to_numpy()

    missing = sorted(set(int(z) for z in zone_ids) - set(table))
    if missing:
        raise ValueError(
            f"roughness zone id(s) {missing} present in "
            f"{Path(cfg.inputs.roughness_zones).name!r} but absent from the "
            f"roughness table {Path(cfg.inputs.roughness_table).name!r} "
            f"(has {sorted(table)})"
        )
    mesh.roughness = np.array([table[int(z)] for z in zone_ids], dtype=float)
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
    """Build the mesh and write the geometry SELAFIN; returns (mesh, slf path)."""
    mesh = build_mesh(cfg, dem_initial_roi)
    slf_path = write_mesh(mesh, cfg.model_path(cfg.geometry_slf),
                          title=f"{cfg.name} geometry")
    return mesh, slf_path
