"""Mesh-quality assessment and validity checks for the generated triangular mesh.

Reports the shape/size metrics that matter for a stable TELEMAC-2D run while
respecting the *intended* anisotropy of the channel (triangles deliberately
elongated along the flow). Ideals follow common hydrodynamic-meshing guidance:

* internal angles ~60 deg, ideally within [30, 90] (avoid obtuse / sliver cells);
* aspect ratio (longest/shortest edge) near 1 — but channel cells are meant to be
  stretched, so channel and floodplain are reported separately (and the shape
  warnings are relaxed for the channel) when a channel mask is supplied;
* smooth size transitions: adjacent triangles should not jump in area by >20%;
* the SHORTEST EDGE is surfaced prominently — it drives the CFL-limited adaptive
  time step.

Validity (fatal) checks: zero-area / degenerate triangles and inverted (mixed
orientation) elements raise; duplicate / orphan nodes, non-manifold edges, extra
boundary loops (hanging gaps) and IPOBO inconsistency (wrong boundary tags) are
flagged. All metrics are vectorised with numpy so they scale to large meshes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from axqua.solvers.telemac.mesh import Mesh

log = logging.getLogger("axqua")


@dataclass
class RegionQuality:
    """Shape statistics for one subset of elements (whole mesh, channel, ...)."""

    label: str
    relaxed: bool          # channel: anisotropy is intended -> don't warn on shape
    n_elem: int
    min_angle: float
    max_angle: float
    frac_below_min: float  # fraction with any angle < min_angle_deg
    frac_obtuse: float     # fraction with any angle > max_angle_deg
    aspect_median: float
    aspect_p95: float
    aspect_max: float
    skew_median: float
    skew_max: float


@dataclass
class QualityReport:
    n_elem: int
    n_nodes: int
    min_edge: float
    median_edge: float
    max_edge: float
    min_area: float
    min_angle_deg: float
    max_angle_deg: float
    area_jump_threshold: float
    frac_area_jump_over: float
    max_area_jump_ratio: float
    regions: list[RegionQuality] = field(default_factory=list)
    # validity
    n_zero_area: int = 0
    n_inverted: int = 0
    n_duplicate_nodes: int = 0
    n_orphan_nodes: int = 0
    n_nonmanifold_edges: int = 0
    n_boundary_loops: int = 0
    ipobo_consistent: bool = True

    @property
    def is_fatal(self) -> bool:
        """Defects that would make TELEMAC reject or mis-read the geometry."""
        return self.n_zero_area > 0 or self.n_inverted > 0 \
            or self.n_duplicate_nodes > 0 or self.n_nonmanifold_edges > 0


# --------------------------------------------------------------------------- #
# per-triangle geometry
# --------------------------------------------------------------------------- #


def _triangle_metrics(x, y, tri):
    """Return (edge_lengths Nx3, angles_deg Nx3, signed_area N)."""
    v0 = np.column_stack([x[tri[:, 0]], y[tri[:, 0]]])
    v1 = np.column_stack([x[tri[:, 1]], y[tri[:, 1]]])
    v2 = np.column_stack([x[tri[:, 2]], y[tri[:, 2]]])
    # edge k is opposite vertex k
    L0 = np.linalg.norm(v1 - v2, axis=1)
    L1 = np.linalg.norm(v2 - v0, axis=1)
    L2 = np.linalg.norm(v0 - v1, axis=1)
    L = np.column_stack([L0, L1, L2])

    def _angle(a, b, c):  # angle opposite side a, between sides b and c
        cos = (b ** 2 + c ** 2 - a ** 2) / (2.0 * b * c)
        return np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))

    with np.errstate(invalid="ignore", divide="ignore"):
        angles = np.column_stack([_angle(L0, L1, L2), _angle(L1, L2, L0),
                                  _angle(L2, L0, L1)])
    signed_area = 0.5 * ((v1[:, 0] - v0[:, 0]) * (v2[:, 1] - v0[:, 1])
                         - (v2[:, 0] - v0[:, 0]) * (v1[:, 1] - v0[:, 1]))
    return L, angles, signed_area


def _region_quality(label, relaxed, L, angles, min_angle_deg, max_angle_deg):
    n = L.shape[0]
    if n == 0:
        return RegionQuality(label, relaxed, 0, float("nan"), float("nan"),
                             0.0, 0.0, float("nan"), float("nan"),
                             float("nan"), float("nan"))
    lmin = L.min(axis=1)
    lmax = L.max(axis=1)
    aspect = np.where(lmin > 0, lmax / np.where(lmin > 0, lmin, 1.0), np.inf)
    amin = angles.min(axis=1)
    amax = angles.max(axis=1)
    skew = np.maximum((amax - 60.0) / 120.0, (60.0 - amin) / 60.0)
    return RegionQuality(
        label=label, relaxed=relaxed, n_elem=int(n),
        min_angle=float(np.nanmin(amin)), max_angle=float(np.nanmax(amax)),
        frac_below_min=float(np.mean(amin < min_angle_deg)),
        frac_obtuse=float(np.mean(amax > max_angle_deg)),
        aspect_median=float(np.median(aspect[np.isfinite(aspect)]))
        if np.isfinite(aspect).any() else float("inf"),
        aspect_p95=float(np.percentile(aspect[np.isfinite(aspect)], 95))
        if np.isfinite(aspect).any() else float("inf"),
        aspect_max=float(np.max(aspect)),
        skew_median=float(np.median(skew)), skew_max=float(np.max(skew)),
    )


# --------------------------------------------------------------------------- #
# adjacency, validity
# --------------------------------------------------------------------------- #


def _edge_table(tri):
    """Unique undirected edges with their occurrence counts (vectorised)."""
    edges = np.vstack([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    uniq, inv, counts = np.unique(edges, axis=0, return_inverse=True,
                                  return_counts=True)
    return edges, uniq, inv, counts


def _area_jumps(tri, areas, inv, counts, threshold):
    """Fraction of interior edges whose two triangles' areas differ by >threshold."""
    tri_idx = np.tile(np.arange(tri.shape[0]), 3)
    order = np.argsort(inv, kind="stable")
    inv_s, tri_s = inv[order], tri_idx[order]
    interior = np.where(counts == 2)[0]
    if interior.size == 0:
        return 0.0, 1.0
    # positions of each edge's first occurrence in the sorted array
    starts = np.searchsorted(inv_s, interior)
    t1 = tri_s[starts]
    t2 = tri_s[starts + 1]
    a1, a2 = np.abs(areas[t1]), np.abs(areas[t2])
    hi = np.maximum(a1, a2)
    lo = np.minimum(a1, a2)
    ratio = np.where(lo > 0, hi / np.where(lo > 0, lo, 1.0), np.inf)
    frac = float(np.mean((ratio - 1.0) > threshold))
    finite = ratio[np.isfinite(ratio)]
    return frac, (float(finite.max()) if finite.size else float("inf"))


def _boundary_loops(uniq, counts):
    """(#loops, #boundary nodes set) from the once-used edges, via union-find."""
    bedges = uniq[counts == 1]
    nodes = np.unique(bedges)
    if nodes.size == 0:
        return 0, nodes
    index = {int(n): i for i, n in enumerate(nodes)}
    parent = list(range(nodes.size))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in bedges:
        ra, rb = find(index[int(a)]), find(index[int(b)])
        if ra != rb:
            parent[ra] = rb
    n_loops = len({find(i) for i in range(nodes.size)})
    return n_loops, nodes


# --------------------------------------------------------------------------- #
# public entry
# --------------------------------------------------------------------------- #


def assess_quality(mesh: "Mesh", *, channel_mask=None, min_angle_deg: float = 30.0,
                   max_angle_deg: float = 90.0,
                   area_jump_threshold: float = 0.20) -> QualityReport:
    """Compute the full :class:`QualityReport` for *mesh*.

    *channel_mask* (optional, (NELEM,) bool) flags the deliberately anisotropic
    channel elements so they are reported in their own, shape-warning-relaxed
    region; the rest are reported as 'floodplain'. Without a mask the whole mesh
    is one region.
    """
    x, y, tri = mesh.x, mesh.y, mesh.triangles
    L, angles, signed = _triangle_metrics(x, y, tri)
    areas = np.abs(signed)

    edges, uniq, inv, counts = _edge_table(tri)
    frac_jump, max_jump = _area_jumps(tri, areas, inv, counts, area_jump_threshold)
    n_loops, bnodes = _boundary_loops(uniq, counts)

    # regions
    regions: list[RegionQuality] = []
    if channel_mask is not None and np.any(channel_mask):
        cm = np.asarray(channel_mask, dtype=bool)
        regions.append(_region_quality("channel", True, L[cm], angles[cm],
                                        min_angle_deg, max_angle_deg))
        regions.append(_region_quality("floodplain", False, L[~cm], angles[~cm],
                                        min_angle_deg, max_angle_deg))
    else:
        regions.append(_region_quality("mesh", False, L, angles,
                                       min_angle_deg, max_angle_deg))

    # validity
    median_area = float(np.median(areas)) if areas.size else 0.0
    zero_tol = max(1e-12, 1e-9 * median_area)
    n_zero = int(np.sum(areas < zero_tol))
    pos, neg = int(np.sum(signed > 0)), int(np.sum(signed < 0))
    n_inverted = min(pos, neg)                       # minority orientation
    coords_r = np.round(np.column_stack([x, y]), 6)
    n_duplicate = int(x.size - np.unique(coords_r, axis=0).shape[0])
    n_orphan = int(x.size - np.unique(tri).size)
    n_nonmanifold = int(np.sum(counts > 2))
    ipobo_consistent = set(int(n) for n in bnodes) == set(
        int(n) for n in np.where(mesh.ipobo > 0)[0]
    )

    return QualityReport(
        n_elem=int(tri.shape[0]), n_nodes=int(x.size),
        min_edge=float(L.min()), median_edge=float(np.median(L)),
        max_edge=float(L.max()), min_area=float(areas.min()),
        min_angle_deg=min_angle_deg, max_angle_deg=max_angle_deg,
        area_jump_threshold=area_jump_threshold,
        frac_area_jump_over=frac_jump, max_area_jump_ratio=max_jump,
        regions=regions, n_zero_area=n_zero, n_inverted=n_inverted,
        n_duplicate_nodes=n_duplicate, n_orphan_nodes=n_orphan,
        n_nonmanifold_edges=n_nonmanifold, n_boundary_loops=n_loops,
        ipobo_consistent=ipobo_consistent,
    )


def log_report(report: QualityReport, logger: logging.Logger = log) -> None:
    """Log the quality report: size summary, per-region shape, and warnings."""
    logger.info("  mesh quality: %d elements, %d nodes", report.n_elem, report.n_nodes)
    logger.info("    edge length  min %.4f m | median %.4f m | max %.4f m",
                report.min_edge, report.median_edge, report.max_edge)
    logger.info("    SHORTEST EDGE = %.4f m is CRITICAL: it sets the CFL-limited "
                "adaptive time step (smaller edge -> smaller stable dt).",
                report.min_edge)
    for r in report.regions:
        if r.n_elem == 0:
            continue
        logger.info("    [%s] %d elems | angles %.1f-%.1f deg | aspect med %.2f "
                    "p95 %.2f max %.2f | skew med %.2f",
                    r.label, r.n_elem, r.min_angle, r.max_angle, r.aspect_median,
                    r.aspect_p95, r.aspect_max, r.skew_median)

    # shape warnings (relaxed for the intentionally anisotropic channel)
    for r in report.regions:
        if r.n_elem == 0 or r.relaxed:
            continue
        if r.frac_below_min > 0.01:
            logger.warning("    %s: %.1f%% of cells have an angle < %.0f deg "
                           "(sliver risk -> numerical instability).",
                           r.label, r.frac_below_min * 100, report.min_angle_deg)
        if r.frac_obtuse > 0.05:
            logger.warning("    %s: %.1f%% of cells have an angle > %.0f deg "
                           "(obtuse cells -> numerical instability).",
                           r.label, r.frac_obtuse * 100, report.max_angle_deg)
        if r.aspect_p95 > 4.0:
            logger.warning("    %s: 95th-percentile aspect ratio %.1f is high "
                           "(target ~1 off-channel; raise floodplain_size or lower "
                           "growth_ratio).", r.label, r.aspect_p95)

    if report.frac_area_jump_over > 0.05:
        logger.warning("    %.1f%% of interior edges jump in area by >%.0f%% "
                       "(max %.1fx). Reduce the mesh size growth (mesh.growth_ratio "
                       "in the anisotropic strategy) for smoother size transitions.",
                       report.frac_area_jump_over * 100,
                       report.area_jump_threshold * 100, report.max_area_jump_ratio)

    # validity
    if report.n_duplicate_nodes:
        logger.warning("    %d duplicate node coordinate(s).", report.n_duplicate_nodes)
    if report.n_orphan_nodes:
        logger.warning("    %d orphan node(s) not used by any triangle.",
                       report.n_orphan_nodes)
    if report.n_nonmanifold_edges:
        logger.warning("    %d non-manifold edge(s) (shared by >2 triangles).",
                       report.n_nonmanifold_edges)
    if report.n_boundary_loops > 1:
        logger.info("    %d boundary loops (1 outer contour + %d island/holes).",
                    report.n_boundary_loops, report.n_boundary_loops - 1)
    if not report.ipobo_consistent:
        logger.warning("    IPOBO boundary numbering does not match the mesh "
                       "contour (risk of wrong boundary tags).")
    if report.n_zero_area or report.n_inverted:
        logger.error("    %d zero-area and %d inverted triangle(s) — invalid geometry.",
                     report.n_zero_area, report.n_inverted)
