"""Stage 3 - boundary conditions (.cli).

Classifies each contour node (in IPOBO order) against the user's liquid-boundary
lines and writes the TELEMAC boundary-conditions file. Codes:

* solid wall                       -> ``2 2 2``  (every non-liquid outer bound)
* inflow  (prescribed flowrate)    -> ``5 5 5``
* outflow, prescribed elevation    -> ``5 4 4``  (``outflow_condition: stage_discharge``
                                                  [default] or ``elevation``)
* outflow, free / Neumann          -> ``4 4 4``  (``outflow_condition: free``)

The liquid-boundary lines come from ``inputs.liquid_boundaries`` (a line layer
whose ``Type (inflow/outflow)`` field tags each line ``inflow`` or ``outflow``;
there may be several of each). They **must coincide with the outer bounds of the
mesh zones** so that contour nodes fall on them. Liquid boundaries are numbered
by first appearance along the contour, matching the order TELEMAC expects for
PRESCRIBED FLOWRATES / PRESCRIBED ELEVATIONS.

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
INFLOW = (5, 5, 5)
OUTFLOW_FREE = (4, 4, 4)   # Neumann / free outflow: nothing prescribed
OUTFLOW_ELEV = (5, 4, 4)   # prescribed downstream water level

# imbalance above this fraction triggers a stability-risk warning
NODE_BALANCE_TOL = 0.10


@dataclass
class LiquidBoundary:
    index: int        # 1-based, in contour-appearance order
    kind: str         # "inflow" | "outflow"
    n_nodes: int


def _outflow_code(cfg: Config) -> tuple[int, int, int]:
    """Free/Neumann outflow -> 4 4 4; stage_discharge / elevation -> 5 4 4."""
    return OUTFLOW_FREE if cfg.hydrodynamics.outflow_condition == "free" else OUTFLOW_ELEV


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

    gdf = gpd.read_file(cfg.inputs.liquid_boundaries)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    type_col = _type_column(gdf)
    out: dict[str, list] = {}
    if type_col is None:
        log.warning("liquid_boundaries %s has no inflow/outflow type column; "
                    "treating every line as inflow",
                    Path(cfg.inputs.liquid_boundaries).name)
        out["inflow"] = list(gdf.geometry.values)
        return {k: unary_union(v) for k, v in out.items()}
    for _, row in gdf.iterrows():
        kind = _normalise_kind(row[type_col])
        if kind not in ("inflow", "outflow"):
            raise ValueError(
                f"liquid_boundaries {Path(cfg.inputs.liquid_boundaries).name!r}: "
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


def classify_nodes(cfg: Config, mesh: Mesh) -> tuple[list[str], list[LiquidBoundary]]:
    """Classify contour nodes; return per-node kind list and liquid boundaries."""
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

    # number contiguous liquid runs by first appearance
    liquids: list[LiquidBoundary] = []
    seen_run = False
    idx = 0
    run_kind = None
    run_len = 0
    for k in kinds + ["wall"]:  # sentinel to flush last run
        if k in ("inflow", "outflow"):
            if k == run_kind:
                run_len += 1
            else:
                if run_kind is not None:
                    idx += 1
                    liquids.append(LiquidBoundary(idx, run_kind, run_len))
                run_kind, run_len = k, 1
            seen_run = True
        else:
            if run_kind is not None:
                idx += 1
                liquids.append(LiquidBoundary(idx, run_kind, run_len))
                run_kind, run_len = None, 0
    if not seen_run:
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
