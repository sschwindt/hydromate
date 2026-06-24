"""Stage 2 — mesh generation, bathymetry, and SELAFIN geometry.

Builds a triangular mesh with gmsh from the ROI boundary polygon and the
breaklines, with per-region (MATID) size refinement, then:

* extracts nodes + triangles,
* walks the mesh boundary to build the ordered contour (drives both TELEMAC's
  IPOBO array and the ``.cli`` row order — they must agree),
* interpolates the clipped DEM onto the nodes (bathymetry),
* assigns each element a MATID (friction zone),
* writes the geometry ``.slf`` via :mod:`tmsetup.selafin`.

The boundary contour and the per-node/element classification are returned so the
boundary-condition stage can tag inflow/outflow nodes consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tmsetup.config import Config
from tmsetup import selafin


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

    _size_fields(cfg, embedded_lines)
    return gmsh


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


def build_mesh(cfg: Config, dem_initial_roi: Path) -> Mesh:
    import gmsh

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

    bottom = _interpolate_bottom(Path(dem_initial_roi), x, y)
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


def run(cfg: Config, dem_initial_roi: Path) -> tuple[Mesh, Path]:
    """Build the mesh and write the geometry SELAFIN; returns (mesh, slf path)."""
    mesh = build_mesh(cfg, dem_initial_roi)
    slf_path = cfg.model_path(cfg.geometry_slf)
    selafin.write_geometry(
        slf_path,
        x=mesh.x, y=mesh.y,
        ikle=mesh.triangles + 1,            # SELAFIN is 1-based
        ipobo=mesh.ipobo, bottom=mesh.bottom,
        friction_id=mesh.node_matid,
        title=f"{cfg.name} geometry",
    )
    return mesh, slf_path
