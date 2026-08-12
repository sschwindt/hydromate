"""Terrain-following, all-hexahedral mesh generation for OpenFOAM river cases.

The mesh is a **structured (i, j, k) grid, warped onto the bed**:

* in plan, a uniform Cartesian lattice of spacing ``dx``, optionally rotated so its
  axes line up with the reach (``align_to_flow``), with the columns whose centre
  falls outside the modelled area simply left out - the survivors form a single
  4-connected block, so the mesh is structured with a hole-free footprint;
* in the vertical, ``n_layers`` sigma layers per column running from the DEM bed to
  a **lid that follows the free surface** at a fixed clearance.

The lid is the part that matters for the air phase. A flat lid high above the
terrain fills the domain with air that has nothing to do and every opportunity to
misbehave: large recirculating air cells drive the Courant number, the atmosphere
patch has to swallow the resulting in/outflow, and the time step collapses on a
phase nobody is interested in. Clamping the lid to ``freeboard`` metres above the
water surface (from the TELEMAC 2D result - see :mod:`hydromate.openfoam.hotstart`)
typically removes 60-90% of the air cells, keeps the atmosphere patch close to the
interface where its ``inletOutlet``/``totalPressure`` pair behaves, and leaves the
free surface room to move.

Every vertical level is computed at the **plan vertices**, never at cell centres, so
two neighbouring columns of different height still share their corner points exactly
and the mesh is conformal by construction - no merging, no snapping, no hanging
nodes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from hydromate.config import Config
from hydromate.core.geodata import dataset
from hydromate.core.structures import (
    SOLID, apply_to_bed, load_structures, solid_footprint,
)
from hydromate.openfoam.polymesh import PolyMesh, assemble, validate

log = logging.getLogger("hydromate")

BED_PATCH = "bed"
ATMOSPHERE_PATCH = "atmosphere"
BANKS_PATCH = "banks"
INLET_PREFIX = "inlet"
OUTLET_PREFIX = "outlet"


# --------------------------------------------------------------------------- #
# plan grid
# --------------------------------------------------------------------------- #


@dataclass
class PlanGrid:
    """The uniform lattice of columns the 3D mesh is extruded from.

    ``col_id`` / ``vert_id`` are the structured lookup tables (``-1`` where the cell
    or vertex is absent); everything downstream addresses the mesh through them.
    """

    dx: float
    angle: float                 # grid rotation [rad], CCW from east
    origin: np.ndarray           # (2,) world xy of the rotated lattice's node (0, 0)
    nx: int
    ny: int
    col_id: np.ndarray           # (ny, nx) int, -1 where the column is absent
    vert_id: np.ndarray          # (ny+1, nx+1) int, -1 where the vertex is unused
    cell_xy: np.ndarray          # (n_columns, 2) world coords of the column centres
    vert_xy: np.ndarray          # (n_vertices, 2) world coords of the used vertices

    @property
    def n_columns(self) -> int:
        return int(self.cell_xy.shape[0])

    @property
    def n_vertices(self) -> int:
        return int(self.vert_xy.shape[0])

    @property
    def keep(self) -> np.ndarray:
        return self.col_id >= 0

    @property
    def cell_area(self) -> float:
        return float(self.dx * self.dx)


def _rotation(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s], [s, c]])


def flow_angle(cfg: Config) -> float:
    """Principal direction of the reach [rad], from the channel centerline.

    Aligning the lattice with it keeps the bulk flow along a grid axis, which is
    where a first/second-order upwind-biased scheme is least diffusive; on a
    diagonal reach it also shrinks the bounding box the lattice has to cover.
    Falls back to 0 (grid axes = easting/northing) with no centerline.
    """
    if cfg.geodata.channel_centerline is None:
        return 0.0
    gdf = dataset(cfg).centerline_frame()
    pts = []
    for geom in gdf.geometry.values:
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            pts.append(np.asarray(part.coords)[:, :2])
    if not pts:
        return 0.0
    xy = np.concatenate(pts)
    xy = xy - xy.mean(axis=0)
    # principal axis = leading eigenvector of the coordinate covariance
    _, _, vt = np.linalg.svd(xy, full_matrices=False)
    return float(np.arctan2(vt[0, 1], vt[0, 0]))


def build_plan_grid(polygon, dx: float, *, angle: float = 0.0,
                    max_columns: int = 4_000_000, blocked=None) -> PlanGrid:
    """Lay a uniform lattice of spacing *dx* over *polygon* (a shapely geometry).

    A column is kept when its centre lies inside the polygon; the kept set is then
    hole-filled and reduced to its largest 4-connected component, so the footprint
    is a single block with no interior voids (an interior void would become a
    spurious internal wall, and a detached island a disconnected mesh region that
    ``decomposePar`` and the pressure solve both dislike).
    """
    import shapely
    from scipy import ndimage

    rot = _rotation(angle)
    coords = np.asarray(polygon.exterior.coords)[:, :2] if hasattr(polygon, "exterior") \
        else np.asarray(polygon.convex_hull.exterior.coords)[:, :2]
    centre = coords.mean(axis=0)
    local = (coords - centre) @ rot          # rotate into grid frame
    lo = local.min(axis=0) - dx
    hi = local.max(axis=0) + dx
    nx = int(np.ceil((hi[0] - lo[0]) / dx))
    ny = int(np.ceil((hi[1] - lo[1]) / dx))
    if nx * ny > max_columns:
        raise ValueError(
            f"a {dx:g} m lattice over this domain needs {nx * ny:,} plan columns "
            f"(cap {max_columns:,}). Coarsen openfoam.cell_size or shrink the domain "
            f"(openfoam.domain: wetted)."
        )
    origin = centre + lo @ rot.T

    i = np.arange(nx)
    j = np.arange(ny)
    uu, vv = np.meshgrid((i + 0.5) * dx, (j + 0.5) * dx)
    world = np.stack([uu.ravel(), vv.ravel()], axis=1) @ rot.T + origin
    keep = shapely.contains_xy(polygon, world[:, 0], world[:, 1]).reshape(ny, nx)

    keep = ndimage.binary_fill_holes(keep)
    if blocked is not None:
        # AFTER the hole fill, never before: a building punched out of the domain is
        # exactly the interior void that binary_fill_holes exists to close, so
        # blanking first would simply be undone.
        solid = shapely.contains_xy(blocked, world[:, 0], world[:, 1]).reshape(ny, nx)
        removed = int((keep & solid).sum())
        keep = keep & ~solid
        if removed:
            log.info("plan grid: %d columns removed by solid structures (%.0f m2)",
                     removed, removed * dx * dx)
    labels, n = ndimage.label(keep)          # 4-connectivity (the default structure)
    if n > 1:
        sizes = ndimage.sum(keep, labels, range(1, n + 1))
        largest = labels == (int(np.argmax(sizes)) + 1)
        dropped = int(keep.sum() - largest.sum())
        keep = largest
        level = log.warning if (blocked is not None and dropped) else log.info
        level("plan grid: kept the largest of %d connected blocks (%d columns, "
              "%d dropped)%s", n, int(keep.sum()), dropped,
              " - a solid structure has cut the domain in two; check that a wall was "
              "not drawn right across the channel" if blocked is not None and dropped
              else "")
    if not keep.any():
        raise ValueError("the plan lattice has no column inside the domain polygon; "
                         "check openfoam.cell_size against the domain size")

    col_id = np.full((ny, nx), -1, dtype=np.int64)
    col_id[keep] = np.arange(int(keep.sum()))

    used = np.zeros((ny + 1, nx + 1), dtype=bool)
    for dj in (0, 1):
        for di in (0, 1):
            used[dj:dj + ny, di:di + nx] |= keep
    vert_id = np.full((ny + 1, nx + 1), -1, dtype=np.int64)
    vert_id[used] = np.arange(int(used.sum()))

    cj, ci = np.nonzero(keep)
    cell_xy = np.stack([(ci + 0.5) * dx, (cj + 0.5) * dx], axis=1) @ rot.T + origin
    vj, vi = np.nonzero(used)
    vert_xy = np.stack([vi * dx, vj * dx], axis=1) @ rot.T + origin

    log.info("plan grid: %d x %d lattice at %.3f m, %d columns kept (%.0f m2)",
             nx, ny, dx, int(keep.sum()), keep.sum() * dx * dx)
    return PlanGrid(dx=dx, angle=angle, origin=origin, nx=nx, ny=ny,
                    col_id=col_id, vert_id=vert_id,
                    cell_xy=cell_xy, vert_xy=vert_xy)


# --------------------------------------------------------------------------- #
# vertical distribution
# --------------------------------------------------------------------------- #


def sigma_levels(height: np.ndarray, n_layers: int, *,
                 bed_layer: float | None = None,
                 expansion: float = 1.0) -> np.ndarray:
    """Per-vertex relative level positions ``s`` in ``[0, 1]``, shape ``(NV, n+1)``.

    Uniform by default. *expansion* > 1 grows the layers from the bed upwards
    (ratio of top to bed layer thickness). *bed_layer* pins the **absolute**
    thickness of the first (bed) layer - the knob that matters here, because
    OpenFOAM's rough wall functions need the first cell centre to stand clear of the
    sand-grain roughness ``Ks``, and on a gravel bed ``Ks`` is a sizeable fraction of
    the flow depth. It is capped at half the local column height so a shallow column
    still gets a mesh.

    Because ``s`` is evaluated per vertex, neighbouring columns still share their
    corner points exactly: the mesh stays conformal even where the layer
    distribution differs.
    """
    height = np.asarray(height, dtype=float)
    nv = height.size
    if n_layers < 1:
        raise ValueError("n_layers must be >= 1")

    if expansion <= 0:
        raise ValueError("expansion must be > 0")
    if np.isclose(expansion, 1.0):
        base = np.ones(n_layers)
    else:
        q = expansion ** (1.0 / max(n_layers - 1, 1))
        base = q ** np.arange(n_layers)
    base = base / base.sum()                                    # (n_layers,)
    s = np.tile(np.concatenate([[0.0], np.cumsum(base)]), (nv, 1))

    if bed_layer is not None and n_layers > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.where(height > 0, np.minimum(bed_layer, 0.5 * height) / height, 0.0)
        # keep the requested distribution above the pinned bed layer
        upper = s[:, 1:] - s[:, 1:2]
        upper = upper / np.where(upper[:, -1:] > 0, upper[:, -1:], 1.0)
        s = np.concatenate([np.zeros((nv, 1)),
                            frac[:, None] + upper * (1.0 - frac)[:, None]], axis=1)
    s[:, -1] = 1.0
    return s


# --------------------------------------------------------------------------- #
# the extruded mesh
# --------------------------------------------------------------------------- #


@dataclass
class OpenFoamMesh:
    """The built 3D mesh plus the bookkeeping the field writers need."""

    polymesh: PolyMesh
    grid: PlanGrid
    n_layers: int
    z: np.ndarray                  # (NV, n_layers+1) vertex levels
    bed: np.ndarray                # (NV,) bed elevation at the plan vertices
    lid: np.ndarray                # (NV,) lid elevation at the plan vertices
    cell_centres: np.ndarray       # (NC, 3)
    cell_column: np.ndarray        # (NC,) column index of each cell
    cell_layer: np.ndarray         # (NC,) 0-based layer index of each cell
    column_bed: np.ndarray         # (n_columns,) bed at the column centres
    column_wse: np.ndarray | None = None      # (n_columns,) hotstart free surface
    column_depth: np.ndarray | None = None    # (n_columns,) hotstart water depth
    column_uv: np.ndarray | None = None       # (n_columns, 2) hotstart depth-averaged U
    bed_ks: np.ndarray | None = None          # (n bed faces,) Nikuradse ks per bed face
    inlet_patches: list[str] = field(default_factory=list)
    outlet_patches: list[str] = field(default_factory=list)
    inlet_discharge: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def n_cells(self) -> int:
        return self.polymesh.n_cells

    @property
    def first_layer_height(self) -> np.ndarray:
        """Thickness of the bed-adjacent layer at every plan vertex [m]."""
        return (self.z[:, 1] - self.z[:, 0])

    @property
    def min_layer_height(self) -> float:
        """Thinnest layer anywhere in the mesh [m] - what limits the time step.

        Not the bed layer: that one is *pinned* (to clear the roughness) and is
        usually the thickest, so the layers above it are thinner wherever a column is
        shallow. The Courant condition sees the thinnest of them.
        """
        return float(np.diff(self.z, axis=1).min())

    @property
    def cell_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """Lower and upper z of every cell [m a.s.l.], averaged over its 4 corners.

        This is what turns a free-surface *elevation* into a VOF ``alpha`` field: the
        submerged fraction of a cell is how much of ``[z_lo, z_hi]`` lies below the
        surface, which puts a one-cell-sharp interface exactly where the 2D model
        says it is instead of the block approximation ``setFields`` would give.
        """
        corners = column_corner_vertices(self.grid)
        z_corner = self.z[corners]                       # (n_columns, 4, n_layers+1)
        level = z_corner.mean(axis=1)                    # (n_columns, n_layers+1)
        return level[:, :-1].ravel(), level[:, 1:].ravel()


def column_corner_vertices(grid: PlanGrid) -> np.ndarray:
    """(n_columns, 4) vertex ids of each column's plan corners, CCW in grid frame."""
    cj, ci = np.nonzero(grid.keep)
    return np.stack([grid.vert_id[cj, ci],
                     grid.vert_id[cj, ci + 1],
                     grid.vert_id[cj + 1, ci + 1],
                     grid.vert_id[cj + 1, ci]], axis=1)


def _levels_to_points(grid: PlanGrid, z: np.ndarray) -> np.ndarray:
    """Flatten the per-vertex level table into the OpenFOAM point list.

    Point ordering is ``vertex * (n_layers + 1) + k``; every downstream index
    derives from that one rule.
    """
    nk = z.shape[1]
    xy = np.repeat(grid.vert_xy, nk, axis=0)
    return np.column_stack([xy, z.ravel()])


def extrude(grid: PlanGrid, bed: np.ndarray, lid: np.ndarray, n_layers: int, *,
            bed_layer: float | None = None, expansion: float = 1.0,
            min_height: float = 0.05) -> tuple[PolyMesh, np.ndarray, np.ndarray,
                                               np.ndarray, np.ndarray, dict]:
    """Extrude *grid* between the per-vertex *bed* and *lid* into a hex mesh.

    Returns ``(polymesh, z, cell_centres, cell_column, cell_layer, side_faces)``
    where ``side_faces`` maps each exposed lateral face group to the arrays the
    patch classifier needs.
    """
    nk = n_layers + 1
    height = np.maximum(lid - bed, min_height)
    s = sigma_levels(height, n_layers, bed_layer=bed_layer, expansion=expansion)
    z = bed[:, None] + height[:, None] * s
    points = _levels_to_points(grid, z)

    corners = column_corner_vertices(grid)          # (nc, 4)
    nc = grid.n_columns

    def pid(vert: np.ndarray, k) -> np.ndarray:
        return vert * nk + k

    # ---- cell centres (mean of the eight corner points) ----------------------
    kk = np.arange(n_layers)
    zc = 0.5 * (z[corners][:, :, kk] + z[corners][:, :, kk + 1])   # (nc, 4, n_layers)
    cell_centres = np.empty((nc * n_layers, 3))
    cell_centres[:, 0] = np.repeat(grid.cell_xy[:, 0], n_layers)
    cell_centres[:, 1] = np.repeat(grid.cell_xy[:, 1], n_layers)
    cell_centres[:, 2] = zc.mean(axis=1).ravel()
    cell_column = np.repeat(np.arange(nc), n_layers)
    cell_layer = np.tile(kk, nc)

    keep = grid.keep
    col = grid.col_id
    ny, nx = keep.shape
    lay = np.arange(n_layers)

    def _stack(v_lo: np.ndarray, v_hi: np.ndarray) -> np.ndarray:
        """Vertical quads spanning plan vertices *v_lo*..*v_hi* over every layer."""
        vlo = np.repeat(v_lo, n_layers)
        vhi = np.repeat(v_hi, n_layers)
        k = np.tile(lay, v_lo.size)
        return np.stack([pid(vlo, k), pid(vhi, k),
                         pid(vhi, k + 1), pid(vlo, k + 1)], axis=1)

    def _cells(cols: np.ndarray) -> np.ndarray:
        return np.repeat(cols, n_layers) * n_layers + np.tile(lay, cols.size)

    internal_quads, internal_own, internal_nei = [], [], []

    # faces between columns adjacent in x (grid frame)
    mx = keep[:, :-1] & keep[:, 1:]
    if mx.any():
        j, i = np.nonzero(mx)
        internal_quads.append(_stack(grid.vert_id[j, i + 1], grid.vert_id[j + 1, i + 1]))
        internal_own.append(_cells(col[j, i]))
        internal_nei.append(_cells(col[j, i + 1]))

    # faces between columns adjacent in y
    my = keep[:-1, :] & keep[1:, :]
    if my.any():
        j, i = np.nonzero(my)
        internal_quads.append(_stack(grid.vert_id[j + 1, i], grid.vert_id[j + 1, i + 1]))
        internal_own.append(_cells(col[j, i]))
        internal_nei.append(_cells(col[j + 1, i]))

    # horizontal faces inside a column
    if n_layers > 1:
        klev = np.tile(np.arange(1, n_layers), nc)
        cc = np.repeat(corners, n_layers - 1, axis=0)
        internal_quads.append(np.stack([pid(cc[:, 0], klev), pid(cc[:, 1], klev),
                                        pid(cc[:, 2], klev), pid(cc[:, 3], klev)], axis=1))
        base = np.repeat(np.arange(nc), n_layers - 1) * n_layers
        internal_own.append(base + klev - 1)
        internal_nei.append(base + klev)

    internal = (np.concatenate(internal_own), np.concatenate(internal_nei),
                np.concatenate(internal_quads, axis=0))

    # ---- boundary faces ------------------------------------------------------
    zero = np.zeros(nc, dtype=np.int64)
    bed_quads = np.stack([pid(corners[:, 0], zero), pid(corners[:, 1], zero),
                          pid(corners[:, 2], zero), pid(corners[:, 3], zero)], axis=1)
    bed_owner = np.arange(nc) * n_layers
    top = np.full(nc, n_layers, dtype=np.int64)
    top_quads = np.stack([pid(corners[:, 0], top), pid(corners[:, 1], top),
                          pid(corners[:, 2], top), pid(corners[:, 3], top)], axis=1)
    top_owner = np.arange(nc) * n_layers + (n_layers - 1)

    padded = np.zeros((ny + 2, nx + 2), dtype=bool)
    padded[1:-1, 1:-1] = keep
    side_quads, side_owner, side_mid = [], [], []
    # (neighbour offset, the two plan vertices of the exposed face)
    exposures = [
        ((0, -1), lambda j, i: (grid.vert_id[j, i], grid.vert_id[j + 1, i])),
        ((0, +1), lambda j, i: (grid.vert_id[j, i + 1], grid.vert_id[j + 1, i + 1])),
        ((-1, 0), lambda j, i: (grid.vert_id[j, i], grid.vert_id[j, i + 1])),
        ((+1, 0), lambda j, i: (grid.vert_id[j + 1, i], grid.vert_id[j + 1, i + 1])),
    ]
    for (dj, di), verts in exposures:
        exposed = keep & ~padded[1 + dj:1 + dj + ny, 1 + di:1 + di + nx]
        if not exposed.any():
            continue
        j, i = np.nonzero(exposed)
        v_lo, v_hi = verts(j, i)
        side_quads.append(_stack(v_lo, v_hi))
        side_owner.append(_cells(col[j, i]))
        side_mid.append(0.5 * (grid.vert_xy[v_lo] + grid.vert_xy[v_hi]))

    sides = {
        "quads": np.concatenate(side_quads, axis=0) if side_quads
        else np.zeros((0, 4), dtype=np.int64),
        "owner": np.concatenate(side_owner) if side_owner
        else np.zeros(0, dtype=np.int64),
        # one midpoint per exposed *column edge*; each spawns n_layers faces
        "midpoint": np.repeat(np.concatenate(side_mid), n_layers, axis=0)
        if side_mid else np.zeros((0, 2)),
    }
    return (points, internal, (bed_quads, bed_owner), (top_quads, top_owner), sides,
            z, cell_centres, cell_column, cell_layer)


# --------------------------------------------------------------------------- #
# patch classification
# --------------------------------------------------------------------------- #


def classify_sides(cfg: Config, midpoints: np.ndarray,
                   tolerance: float) -> tuple[np.ndarray, list[str], dict[str, float]]:
    """Tag each exposed lateral face as an inlet, an outlet, or a bank wall.

    Reuses the case's ``boundaries.liquid_boundaries`` layer - the same lines that
    drive the TELEMAC ``.cli`` - so the 2D and 3D models take their flow in and out
    at exactly the same places. Each *line* becomes its own patch (``inlet-1``,
    ``inlet-2``, ...) so a reach with two feeding branches can prescribe each
    branch's own discharge, and the per-line ``Target flow`` field (when present)
    supplies it.
    """
    import shapely

    from hydromate.boundary import liquid_line_details, liquid_lines

    labels = np.full(midpoints.shape[0], BANKS_PATCH, dtype=object)
    names: list[str] = []
    discharges: dict[str, float] = {}
    if cfg.boundaries.liquid_boundaries is None or midpoints.shape[0] == 0:
        return labels, names, discharges

    details = liquid_line_details(cfg)
    if not details:
        merged = liquid_lines(cfg)
        details = [{"kind": k, "discharge": None, "geom": g} for k, g in merged.items()]

    pts = shapely.points(midpoints[:, 0], midpoints[:, 1])
    best = np.full(midpoints.shape[0], np.inf)
    counts: dict[str, int] = {}
    n_in = n_out = 0
    for entry in details:
        kind = entry["kind"]
        if kind == "inflow":
            n_in += 1
            name = f"{INLET_PREFIX}-{n_in}"
        else:
            n_out += 1
            name = f"{OUTLET_PREFIX}-{n_out}"
        dist = shapely.distance(pts, entry["geom"])
        hit = (dist <= tolerance) & (dist < best)
        if hit.any():
            labels[hit] = name
            best[hit] = dist[hit]
            names.append(name)
            counts[name] = int(hit.sum())
            if entry.get("discharge") is not None:
                discharges[name] = float(entry["discharge"])
    for name, n in counts.items():
        log.info("patch %s: %d lateral faces within %.2f m of its liquid line",
                 name, n, tolerance)
    return labels, names, discharges


# --------------------------------------------------------------------------- #
# inputs sampled onto the grid
# --------------------------------------------------------------------------- #


def _roughness_at(cfg: Config, xy: np.ndarray) -> np.ndarray | None:
    """Nikuradse ``ks`` [m] at arbitrary plan points, from the roughness zones.

    The same ``geodata.roughness_zones`` + ``roughness_table`` pair the TELEMAC build
    turns into ``FRIC_ID``/``.tbl`` rows, so the 2D and 3D models are rough in the
    same places; ``None`` when the case has no zonation (the caller then falls back
    to a single ``friction`` value).
    """
    if cfg.geodata.roughness_zones is None or cfg.geodata.roughness_table is None:
        return None
    import shapely
    from scipy.spatial import cKDTree

    table = dataset(cfg).roughness_table()
    zones = dataset(cfg).roughness_zones()
    field_name = cfg.mesh.roughness_zone_field
    if field_name not in zones.columns:
        log.warning("roughness zones have no %r column; skipping per-face ks",
                    field_name)
        return None
    ids = zones[field_name].astype(int).to_numpy()
    ks = np.array([table.get(int(i), np.nan) for i in ids], dtype=float)

    out = np.full(xy.shape[0], np.nan)
    pts = shapely.points(xy[:, 0], xy[:, 1])
    tree = shapely.STRtree(zones.geometry.values)
    hits = tree.query(pts, predicate="within")
    if hits.size:
        out[hits[0]] = ks[hits[1]]
    missing = np.isnan(out)
    if missing.any():  # outside every polygon -> nearest zone centroid
        cent = np.array([[g.x, g.y] for g in zones.geometry.centroid.values])
        _, idx = cKDTree(cent).query(xy[missing])
        out[missing] = ks[idx]
    return out


def _smooth_lid(grid: PlanGrid, values: np.ndarray, *, passes: int,
                dilate: int) -> np.ndarray:
    """Grey-dilate then average the lid over the plan lattice.

    A lid taken verbatim from a 2D result is as ragged as that result: local dips
    over shallow cells would pinch the air layer to nothing and make thin, badly
    shaped cells. Dilating first (a running maximum) guarantees the smoothed lid is
    never *below* the surface it came from, then averaging removes the steps.
    """
    from scipy import ndimage

    used = grid.vert_id >= 0
    field2d = np.full(grid.vert_id.shape, np.nan)
    field2d[used] = values
    if dilate > 0:
        filled = np.where(np.isnan(field2d), -np.inf, field2d)
        field2d = np.where(used, ndimage.grey_dilation(filled, size=2 * dilate + 1),
                           np.nan)
    for _ in range(max(passes, 0)):
        filled = np.where(np.isnan(field2d), 0.0, field2d)
        weight = ndimage.uniform_filter(used.astype(float), size=3, mode="nearest")
        avg = ndimage.uniform_filter(filled, size=3, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            field2d = np.where(used & (weight > 0), avg / weight, field2d)
    return field2d[used]


def _domain_polygon(cfg: Config, state, *, domain: str, wet_margin: float,
                    wet_depth: float):
    """The plan footprint the lattice covers: the ROI, or the wetted corridor."""
    from shapely.ops import unary_union

    from hydromate.mesh import roi_polygon

    roi = roi_polygon(cfg)
    if domain == "roi" or state is None:
        return roi, "the full ROI boundary"
    wet = state.wet_footprint(wet_depth=wet_depth, buffer=wet_margin)
    if wet is None or wet.is_empty:
        log.warning("no wetted footprint in the 2D result; meshing the full ROI")
        return roi, "the full ROI boundary (no wetted footprint found)"
    clipped = unary_union([wet]).intersection(roi)
    if clipped.is_empty:
        return roi, "the full ROI boundary (wetted footprint missed the ROI)"
    if clipped.geom_type == "MultiPolygon":
        clipped = max(clipped.geoms, key=lambda g: g.area)
    return clipped, (f"the 2D wetted extent (H > {wet_depth:g} m) buffered by "
                     f"{wet_margin:g} m, clipped to the ROI")


# --------------------------------------------------------------------------- #
# the public builder
# --------------------------------------------------------------------------- #


def build_mesh(cfg: Config, *, state=None, dem: str | Path | None = None) -> OpenFoamMesh:
    """Build the terrain-following hex mesh for *cfg*'s OpenFOAM case.

    *state* is an optional :class:`~hydromate.openfoam.hotstart.State2D` (the
    converged TELEMAC 2D result). With one, the lid follows the free surface and the
    footprint can be trimmed to the wetted corridor; without one, the lid is flat and
    the footprint is the whole ROI - correct, but far more air.
    """
    of = cfg.openfoam
    dem = Path(dem) if dem is not None else Path(cfg.geodata.dem_initial)
    notes: list[str] = []

    polygon, how = _domain_polygon(cfg, state, domain=of.domain,
                                   wet_margin=of.wet_margin,
                                   wet_depth=cfg.hydrodynamics.wet_depth)
    notes.append(f"footprint: {how}")

    structures = load_structures(cfg)
    blocked = solid_footprint(structures)
    if blocked is not None:
        notes.append(f"{sum(1 for s in structures if s.mode == SOLID)} solid "
                     "structure(s) removed from the domain: their sides become "
                     "no-slip walls from bed to lid")

    angle = flow_angle(cfg) if of.align_to_flow else 0.0
    if of.align_to_flow:
        notes.append(f"lattice rotated {np.degrees(angle):+.1f} deg onto the reach axis")
    grid = build_plan_grid(polygon, of.cell_size, angle=angle,
                           max_columns=of.max_plan_columns, blocked=blocked)

    from hydromate.mesh import sample_raster_at

    bed = sample_raster_at(dem, grid.vert_xy[:, 0], grid.vert_xy[:, 1])
    column_bed = sample_raster_at(dem, grid.cell_xy[:, 0], grid.cell_xy[:, 1])
    if structures:
        # overflow structures ARE terrain the DEM did not have; raising the bed keeps
        # the plan mesh unchanged and lets water pass over the crest
        bed, touched = apply_to_bed(structures, grid.vert_xy, bed)
        column_bed, _ = apply_to_bed(structures, grid.cell_xy, column_bed)
        if touched.any():
            notes.append(f"{int(touched.sum())} mesh vertices raised to an overflow "
                         "structure crest")

    # ---- lid ---------------------------------------------------------------
    column_wse = column_depth = column_uv = None
    if state is not None:
        wse_v = state.sample_surface(grid.vert_xy)
        column_wse, column_depth, column_uv = state.sample_columns(grid.cell_xy)
        wse_v = np.where(np.isfinite(wse_v), wse_v, bed)
        wse_v = np.maximum(wse_v, bed)
        lid = wse_v + of.freeboard
    else:
        base = float(np.nanmax(bed))
        lid = np.full_like(bed, base + of.freeboard)
        notes.append("no 2D hotstart: flat lid, so the domain carries the full air column")
    if of.lid == "flat":
        lid = np.full_like(lid, float(np.nanmax(lid)))
        notes.append(f"flat lid at {lid[0]:.3f} m a.s.l.")
    if of.lid_elevation is not None:
        lid = np.full_like(lid, float(of.lid_elevation))
        notes.append(f"lid pinned to the configured {of.lid_elevation:g} m a.s.l.")
    lid = np.maximum(lid, bed + of.min_column_height)
    if of.lid == "follow":
        lid = _smooth_lid(grid, lid, passes=of.lid_smoothing,
                          dilate=of.lid_dilation)
        lid = np.maximum(lid, bed + of.min_column_height)

    # ---- bed layer sized against the roughness ------------------------------
    bed_ks = _roughness_at(cfg, grid.cell_xy)
    bed_layer = of.bed_layer_height
    if bed_layer is None and of.auto_bed_layer and bed_ks is not None:
        # OpenFOAM's nutkRoughWallFunction places the first grid point on a log law
        # whose origin is displaced into the roughness; that is only meaningful with
        # the first cell CENTRE above the roughness crests, y1 > ks. The centre sits
        # at half the layer, so the layer must be ~2*ks - which is why a river mesh
        # cannot simply be refined towards the bed the way an aerofoil mesh can.
        #
        # The reference ks is the median over the WETTED columns, not over the whole
        # footprint: the dry floodplain zones carry the vegetation ks (isar: 0.5 m),
        # which has no bearing on a wall function under water and would otherwise
        # pin the bed layer to half the column height everywhere.
        wet_cols = (column_depth > cfg.hydrodynamics.wet_depth) if \
            column_depth is not None else np.ones(bed_ks.shape, dtype=bool)
        sample = bed_ks[wet_cols] if wet_cols.any() else bed_ks
        bed_layer = 2.0 * float(np.nanmedian(sample))
        # ...but it must still leave room for the layers above it, or the vertical
        # distribution degenerates into one thick cell plus a stack of slivers.
        ceiling = float(np.nanmedian(np.maximum(lid - bed, of.min_column_height))) / 3.0
        if bed_layer > ceiling:
            notes.append(
                f"bed layer wanted {bed_layer:.3f} m (2x the wetted median ks) but is "
                f"clipped to {ceiling:.3f} m to leave room for the layers above it - "
                "the wall function is not admissible at this roughness/depth ratio "
                "(see the quality report)")
            bed_layer = ceiling
        else:
            notes.append(
                f"bed layer pinned to {bed_layer:.3f} m (2x the wetted median ks, so "
                "the first cell centre clears the roughness crests)")

    (points, internal, (bed_quads, bed_owner), (top_quads, top_owner), sides,
     z, cell_centres, cell_column, cell_layer) = extrude(
        grid, bed, lid, of.n_layers, bed_layer=bed_layer,
        expansion=of.layer_expansion, min_height=of.min_column_height)

    labels, liquid_names, discharges = classify_sides(
        cfg, sides["midpoint"], of.boundary_tolerance or 1.5 * of.cell_size)

    boundary = [(BED_PATCH, "wall", bed_owner, bed_quads)]
    for name in liquid_names:
        sel = labels == name
        boundary.append((name, "patch", sides["owner"][sel], sides["quads"][sel]))
    banks = labels == BANKS_PATCH
    boundary.append((BANKS_PATCH, "wall", sides["owner"][banks], sides["quads"][banks]))
    boundary.append((ATMOSPHERE_PATCH, "patch", top_owner, top_quads))

    mesh = assemble(points, internal, boundary, cell_centres)
    problems = validate(mesh)
    for problem in problems:
        log.error("polyMesh: %s", problem)
    if problems:
        raise ValueError("the generated polyMesh is structurally invalid: "
                         + "; ".join(problems))

    result = OpenFoamMesh(
        polymesh=mesh, grid=grid, n_layers=of.n_layers, z=z, bed=bed, lid=lid,
        cell_centres=cell_centres, cell_column=cell_column, cell_layer=cell_layer,
        column_bed=column_bed, column_wse=column_wse, column_depth=column_depth,
        column_uv=column_uv, bed_ks=bed_ks,
        inlet_patches=[n for n in liquid_names if n.startswith(INLET_PREFIX)],
        outlet_patches=[n for n in liquid_names if n.startswith(OUTLET_PREFIX)],
        inlet_discharge=discharges, notes=notes,
    )
    log.info("OpenFOAM mesh: %d cells (%d columns x %d layers), %d faces",
             mesh.n_cells, grid.n_columns, of.n_layers, mesh.n_faces)
    return result
