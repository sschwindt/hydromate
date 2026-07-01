"""Stage 3 - boundary conditions (.cli).

Classifies each contour node (in IPOBO order) against the user's liquid-boundary
lines and writes the TELEMAC boundary-conditions file. Codes:

* solid wall                       -> ``2 2 2``  (every non-liquid outer bound)
* inflow  (prescribed flowrate)    -> ``4 5 5``  (depth free, velocity from Q)
* outflow, prescribed elevation    -> ``5 4 4``  (``outflow_condition: stage_discharge``
                                                  [default] or ``elevation``)
* outflow, free / Neumann          -> ``4 4 4``  (``outflow_condition: free``)

The liquid-boundary lines come from ``boundaries.liquid_boundaries`` (a line layer
whose ``Type (inflow/outflow)`` field tags each line ``inflow`` or ``outflow``;
there may be several of each). They **must coincide with the outer bounds of the
mesh zones** so that contour nodes fall on them. Liquid boundaries are numbered
exactly the way TELEMAC's ``FRONT2`` does - per contour loop, starting at the
south-westernmost node and walking the contour with the domain on its left,
merging a run that wraps the loop start (see :func:`_front2_runs`) - so the
PRESCRIBED FLOWRATES / PRESCRIBED ELEVATIONS line up with the boundary numbering
TELEMAC derives from the geometry.

For numerical stability the total inflow-node count should be within ~10% of the
total outflow-node count; otherwise a stability-risk warning is logged (raise the
mesh resolution near the boundaries or adjust the line lengths to rebalance).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hydromate.config import Config
from hydromate.mesh import Mesh, _anisotropic_enabled

log = logging.getLogger("hydromate")

WALL = (2, 2, 2)
# prescribed-discharge inflow: LIHBOR=4 (depth FREE, computed), LIUBOR/LIVBOR=5
# (velocity prescribed from the imposed Q + profile). LIHBOR must be 4, not 5:
# a 5 prescribes the depth from PRESCRIBED ELEVATIONS, which is 0 for an inflow,
# so the inflow would be forced to a zero water surface (dry) and TELEMAC's
# DEBIMP aborts ("PROBLEM ON BOUNDARY ... CHECK THE WATER DEPTHS").
INFLOW = (4, 5, 5)
OUTFLOW_FREE = (4, 4, 4)   # Neumann / free outflow: nothing prescribed
OUTFLOW_ELEV = (5, 4, 4)   # prescribed downstream water level

# imbalance above this fraction triggers a stability-risk warning
NODE_BALANCE_TOL = 0.10


@dataclass
class LiquidBoundary:
    index: int        # 1-based, in contour-appearance order
    kind: str         # "inflow" | "outflow"
    n_nodes: int


def dump_liquid_boundaries(liquids: list["LiquidBoundary"], path: str | Path) -> Path:
    """Serialize the FRONT2-ordered liquid boundaries to JSON.

    Written during the build (:mod:`hydromate.pipeline`) so the standalone unsteady /
    3D scripts recover the exact TELEMAC boundary numbering (index/kind/node count)
    without rebuilding the mesh - all :func:`load_liquid_boundaries` needs to write
    the hydrograph liquid-boundaries file and the prescribed-value arrays.
    """
    import json

    path = Path(path)
    path.write_text(json.dumps(
        [{"index": lb.index, "kind": lb.kind, "n_nodes": lb.n_nodes} for lb in liquids],
        indent=2) + "\n")
    return path


def load_liquid_boundaries(path: str | Path) -> list["LiquidBoundary"]:
    """Load the liquid boundaries dumped by :func:`dump_liquid_boundaries`."""
    import json

    data = json.loads(Path(path).read_text())
    return [LiquidBoundary(index=int(d["index"]), kind=str(d["kind"]),
                           n_nodes=int(d["n_nodes"])) for d in data]


def _outflow_code(cfg: Config) -> tuple[int, int, int]:
    """Free/Neumann outflow -> 4 4 4; stage_discharge / elevation -> 5 4 4."""
    return OUTFLOW_FREE if cfg.boundaries.outflow_condition == "free" else OUTFLOW_ELEV


def _normalise_kind(value) -> str:
    """Map a free-text type tag to 'inflow' / 'outflow' (else the lowercased tag)."""
    s = str(value).strip().lower()
    if "out" in s:
        return "outflow"
    if "in" in s:
        return "inflow"
    return s


def _type_column(gdf) -> str | None:
    """Find the attribute holding the inflow/outflow tag.

    Matches a column whose name mentions type/kind/inflow/outflow/stringdef (so
    the Inn layer's ``Type (inflow/outflow)`` is picked up); falls back to the
    sole non-geometry column when there is exactly one.
    """
    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    for c in cols:
        cl = c.lower()
        if any(k in cl for k in ("type", "kind", "inflow", "outflow", "stringdef")):
            return c
    return cols[0] if len(cols) == 1 else None


def _load_liquid_lines(cfg: Config):
    """Return dict kind -> shapely geometry (union of that kind's lines)."""
    import geopandas as gpd
    from shapely.ops import unary_union

    gdf = gpd.read_file(cfg.boundaries.liquid_boundaries)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    type_col = _type_column(gdf)
    out: dict[str, list] = {}
    if type_col is None:
        log.warning("liquid_boundaries %s has no inflow/outflow type column; "
                    "treating every line as inflow",
                    Path(cfg.boundaries.liquid_boundaries).name)
        out["inflow"] = list(gdf.geometry.values)
        return {k: unary_union(v) for k, v in out.items()}
    for _, row in gdf.iterrows():
        kind = _normalise_kind(row[type_col])
        if kind not in ("inflow", "outflow"):
            raise ValueError(
                f"liquid_boundaries {Path(cfg.boundaries.liquid_boundaries).name!r}: "
                f"line tagged {row[type_col]!r} in column {type_col!r} is neither "
                "'inflow' nor 'outflow'"
            )
        out.setdefault(kind, []).append(row.geometry)
    return {k: unary_union(v) for k, v in out.items()}


def _match_tolerance(cfg: Config) -> float:
    """Distance below which a contour node is taken to lie on a liquid line.

    Tied to the local boundary edge length so it captures on-line nodes without
    grabbing wall nodes deep past the inflow/outflow line ends: ~2 floodplain
    edges for the anisotropic mesh, else the isotropic default/breakline size.
    """
    if _anisotropic_enabled(cfg):
        return max(cfg.mesh.floodplain_size, cfg.mesh.channel_size) * 2.0
    return max(cfg.mesh.default_size, cfg.mesh.breakline_size) * 1.5


def _warn_node_balance(liquids: list[LiquidBoundary]) -> None:
    """Log a stability-risk warning when inflow/outflow node counts differ >10%."""
    n_in = sum(lb.n_nodes for lb in liquids if lb.kind == "inflow")
    n_out = sum(lb.n_nodes for lb in liquids if lb.kind == "outflow")
    if n_in == 0 or n_out == 0:
        log.warning(
            "STABILITY RISK: liquid boundaries have %d inflow and %d outflow "
            "nodes - a model needs at least one of each. Check that the "
            "liquid-boundary lines are tagged and coincide with the mesh bounds.",
            n_in, n_out,
        )
        return
    imbalance = abs(n_in - n_out) / max(n_in, n_out)
    if imbalance > NODE_BALANCE_TOL:
        log.warning(
            "STABILITY RISK: inflow nodes (%d) and outflow nodes (%d) differ by "
            "%.0f%% (> %.0f%%). Adjust the mesh resolution near the boundaries or "
            "the inflow/outflow line lengths so they carry a similar node count.",
            n_in, n_out, imbalance * 100, NODE_BALANCE_TOL * 100,
        )
    else:
        log.info("liquid-boundary node balance OK: %d inflow vs %d outflow nodes "
                 "(%.0f%% difference)", n_in, n_out, imbalance * 100)


def _front2_runs(loop_nodes, loop_kinds: list[str], x, y) -> list[tuple[str, int]]:
    """Liquid/wall runs of one closed contour loop, in TELEMAC ``FRONT2`` order.

    TELEMAC numbers liquid boundaries (``bief/front2.f``) by starting at each
    loop's **south-westernmost** node (min ``x+y``, ties broken by min ``y``) and
    walking the contour with the domain on its left (the outer loop is traversed
    counter-clockwise, as ``KP1BOR`` follows the CCW element node order), counting
    a new liquid boundary at every transition into a liquid segment and merging a
    run that wraps the loop start. We replay exactly that here so our liquid-
    boundary numbering matches the order TELEMAC assigns - otherwise the
    PRESCRIBED FLOWRATES / ELEVATIONS land on the wrong boundary (inflow Q and
    outflow H swapped) or a single boundary that straddles the loop start is split
    into two.
    """
    import numpy as np

    n = len(loop_nodes)
    if n == 0:
        return []
    xs = np.asarray(x)[loop_nodes]
    ys = np.asarray(y)[loop_nodes]

    # orient domain-on-left (outer loop CCW, signed area > 0); islands are all
    # wall, so their orientation does not affect liquid numbering.
    area2 = float(np.sum(xs * np.roll(ys, -1) - np.roll(xs, -1) * ys))
    order = np.arange(n)[::-1] if area2 < 0 else np.arange(n)
    oxs, oys = xs[order], ys[order]
    okinds = [loop_kinds[i] for i in order]

    # south-westernmost start (FRONT2's IDEP): min x+y, ties broken by min y
    key = oxs + oys
    eps = (key.max() - key.min()) * 1e-4
    cand = np.flatnonzero(np.abs(key - key.min()) <= eps)
    start = int(cand[np.argmin(oys[cand])])
    rkinds = [okinds[(start + j) % n] for j in range(n)]

    runs: list[list] = []
    for k in rkinds:
        if runs and runs[-1][0] == k:
            runs[-1][1] += 1
        else:
            runs.append([k, 1])
    # the loop is cyclic: a run that wraps the start is one boundary, not two
    if len(runs) > 1 and runs[0][0] == runs[-1][0]:
        runs[0][1] += runs[-1][1]
        runs.pop()
    return [(k, c) for k, c in runs]


def _number_liquid_boundaries(mesh: Mesh, kinds: list[str]) -> list[LiquidBoundary]:
    """Number the liquid boundaries per contour loop in TELEMAC ``FRONT2`` order."""
    import numpy as np

    bn = np.asarray(mesh.boundary_nodes)
    loops = getattr(mesh, "boundary_loops", None)
    loop_lengths = ([int(bn.size)] if loops is None or len(loops) == 0
                    else [int(n) for n in loops])

    liquids: list[LiquidBoundary] = []
    off = 0
    for length in loop_lengths:
        loop_nodes = bn[off:off + length]
        loop_kinds = kinds[off:off + length]
        off += length
        for kind, count in _front2_runs(loop_nodes, loop_kinds, mesh.x, mesh.y):
            if kind in ("inflow", "outflow"):
                liquids.append(LiquidBoundary(len(liquids) + 1, kind, count))
    return liquids


def classify_nodes(cfg: Config, mesh: Mesh) -> tuple[list[str], list[LiquidBoundary]]:
    """Classify contour nodes; return per-node kind list and liquid boundaries.

    ``kinds`` stays in ``mesh.boundary_nodes`` order (so it drives the ``.cli`` row
    order, which must match the geometry's IPOBO). The returned ``LiquidBoundary``
    list is numbered the way TELEMAC's ``FRONT2`` does (see
    :func:`_front2_runs`), so the steering file's per-boundary prescribed values
    line up with the boundary numbering TELEMAC derives from the geometry.
    """
    from shapely.geometry import Point

    lines = _load_liquid_lines(cfg)
    tol = _match_tolerance(cfg)

    kinds: list[str] = []
    for node in mesh.boundary_nodes:
        p = Point(mesh.x[node], mesh.y[node])
        best_kind, best_dist = "wall", tol
        for kind, geom in lines.items():
            d = geom.distance(p)
            if d < best_dist:
                best_kind, best_dist = kind, d
        kinds.append(best_kind)

    liquids = _number_liquid_boundaries(mesh, kinds)
    if not liquids:
        raise ValueError(
            "No contour nodes matched the liquid-boundary lines. Check that the "
            "liquid_boundaries lines coincide with the mesh-zone outer bounds and "
            f"the matching tolerance (~{tol:.1f} m) suits the mesh resolution."
        )
    _warn_node_balance(liquids)
    return kinds, liquids


def write_cli(cfg: Config, mesh: Mesh) -> tuple[Path, list[LiquidBoundary]]:
    kinds, liquids = classify_nodes(cfg, mesh)
    code = {"wall": WALL, "inflow": INFLOW, "outflow": _outflow_code(cfg)}

    rows = []
    for rank, (node, kind) in enumerate(zip(mesh.boundary_nodes, kinds), start=1):
        c = code[kind]
        n_global = int(node) + 1  # SELAFIN/TELEMAC 1-based
        comment = "" if kind == "wall" else kind
        rows.append(
            f"{c[0]} {c[1]} {c[2]}  0.000 0.000 0.000 0.000  2  0.000 0.000 0.000  "
            f"{n_global:>10}  {rank:>10}   # {comment}"
        )
    path = cfg.model_path(cfg.boundary_cli)
    path.write_text("\n".join(rows) + "\n")
    return path, liquids
