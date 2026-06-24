"""Stage 3 — boundary conditions (.cli).

Classifies each contour node (in IPOBO order) against the user's liquid-boundary
lines and writes the TELEMAC boundary-conditions file. Codes follow the working
Inn model:

* solid wall                       -> ``2 2 2``
* inflow  (prescribed flowrate)    -> ``5 5 5``
* outflow (prescribed elevation)   -> ``5 4 4``

Liquid boundaries are numbered by first appearance along the contour, matching
the order TELEMAC expects for PRESCRIBED FLOWRATES / PRESCRIBED ELEVATIONS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tmsetup.config import Config
from tmsetup.mesh import Mesh

WALL = (2, 2, 2)
INFLOW = (5, 5, 5)
OUTFLOW = (5, 4, 4)


@dataclass
class LiquidBoundary:
    index: int        # 1-based, in contour-appearance order
    kind: str         # "inflow" | "outflow"
    n_nodes: int


def _load_liquid_lines(cfg: Config):
    """Return dict kind -> shapely geometry (union of that kind's lines)."""
    import geopandas as gpd
    from shapely.ops import unary_union

    gdf = gpd.read_file(cfg.inputs.liquid_boundaries)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    type_col = next((c for c in gdf.columns if c.lower() in ("type", "kind", "stringdef")),
                    None)
    out: dict[str, object] = {}
    if type_col is None:
        out["inflow"] = unary_union(gdf.geometry.values)
        return out
    for _, row in gdf.iterrows():
        kind = str(row[type_col]).strip().lower()
        kind = "inflow" if "in" in kind else "outflow" if "out" in kind else kind
        out.setdefault(kind, [])
        out[kind].append(row.geometry)
    return {k: unary_union(v) for k, v in out.items()}


def classify_nodes(cfg: Config, mesh: Mesh) -> tuple[list[str], list[LiquidBoundary]]:
    """Classify contour nodes; return per-node kind list and liquid boundaries."""
    from shapely.geometry import Point

    lines = _load_liquid_lines(cfg)
    tol = max(cfg.mesh.default_size, cfg.mesh.breakline_size) * 1.5

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
            "No contour nodes matched the liquid-boundary lines. Check that "
            "liquid_boundaries overlaps the ROI boundary and the tolerance "
            f"(~{tol:.1f} m) suits the mesh resolution."
        )
    return kinds, liquids


def write_cli(cfg: Config, mesh: Mesh) -> tuple[Path, list[LiquidBoundary]]:
    kinds, liquids = classify_nodes(cfg, mesh)
    code = {"wall": WALL, "inflow": INFLOW, "outflow": OUTFLOW}

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
