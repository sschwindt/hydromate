"""Liquid-boundary classification, outflow type, and node-balance warning.

Builds a rectangular contour mesh plus a ``boundaries.liquid_boundaries`` line layer
with a ``Type (inflow/outflow)`` field (left edge = inflow, right edge =
outflow), then checks:

* the type column is detected and lines tagged inflow/outflow (regression: the
  literal ``Type (inflow/outflow)`` header must not fall back to "all inflow");
* every non-liquid contour node is a solid wall (``2 2 2``);
* the default outflow is free / Neumann (``4 4 4``); ``outflow_condition:
  elevation`` switches it to ``5 4 4``;
* an inflow/outflow node-SPACING (resolution) mismatch, or an under-resolved
  boundary, logs a STABILITY RISK warning (unequal raw counts alone do not);
* internal losing/gaining lines are distributed across the mesh nodes under them.

Requires the ``hydromate-env`` environment (geopandas). No TELEMAC needed.

Run directly:  mamba run -n hydromate-env python tests/test_boundary.py
Or via pytest: mamba run -n hydromate-env pytest tests/test_boundary.py
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

X0, Y0, W, H = 700000.0, 5340000.0, 200.0, 60.0


def _rect_mesh(n_left: int, n_right: int, n_horiz: int = 9, roll: int = 0):
    """Rectangular contour: *n_left* inflow nodes on the left edge, *n_right*
    outflow nodes on the right edge, walls top/bottom. Returns a Mesh whose
    boundary_nodes are ordered around the loop.

    *roll* rotates the loop start by that many nodes, so a single liquid edge can
    be made to straddle the loop start (boundary_nodes index 0) - the wrap-around
    that used to split one boundary into two."""
    from hydromate.mesh import Mesh

    bottom = [(x, Y0) for x in np.linspace(X0, X0 + W, n_horiz)[1:-1]]
    right = [(X0 + W, y) for y in np.linspace(Y0, Y0 + H, n_right)]
    top = [(x, Y0 + H) for x in np.linspace(X0 + W, X0, n_horiz)[1:-1]]
    left = [(X0, y) for y in np.linspace(Y0 + H, Y0, n_left)]
    ring = bottom + right + top + left            # loop order
    if roll:
        ring = ring[roll:] + ring[:roll]
    xy = np.array(ring)
    npoin = len(ring)
    return Mesh(
        x=xy[:, 0], y=xy[:, 1],
        triangles=np.array([[0, 1, 2]]),          # unused by classification
        bottom=np.zeros(npoin), ipobo=np.arange(1, npoin + 1),
        boundary_nodes=np.arange(npoin),
        element_matid=np.ones(1, dtype=int), node_matid=np.ones(npoin, dtype=int),
        boundary_loops=np.array([npoin]),
    )


def _write_cfg(d: Path, outflow_condition: str = "free") -> Path:
    import geopandas as gpd
    from shapely.geometry import LineString

    geo = d / "geo"
    geo.mkdir(parents=True, exist_ok=True)
    inflow = LineString([(X0, Y0), (X0, Y0 + H)])           # left edge
    outflow = LineString([(X0 + W, Y0), (X0 + W, Y0 + H)])  # right edge
    gpd.GeoDataFrame(
        {"Type (inflow/outflow)": ["inflow", "outflow"]},
        geometry=[inflow, outflow], crs="EPSG:25832",
    ).to_file(geo / "liquid-boundaries.gpkg", driver="GPKG")

    cfg_yaml = d / f"bnd-{outflow_condition}.yml"
    cfg_yaml.write_text(f"""
project:
  name: bnd-test
  crs_epsg: 25832
telemac:
  pysource: {geo / "liquid-boundaries.gpkg"}   # not sourced here
geodata:
  dem_initial: geo/dem.tif            # dummy (existence not checked here)
  boundary: geo/boundary.shp
boundaries:
  liquid_boundaries: geo/liquid-boundaries.gpkg
  outflow_condition: {outflow_condition}
""")
    return cfg_yaml


def run_boundary_test(tmp: Path) -> None:
    from hydromate import boundary
    from hydromate.config import load_config

    cfg = load_config(_write_cfg(tmp, "free"))
    cfg.ensure_dirs()

    # balanced contour (7 inflow / 7 outflow nodes) -> classification
    mesh = _rect_mesh(n_left=7, n_right=7)
    kinds, liquids = boundary.classify_nodes(cfg, mesh)

    assert kinds.count("inflow") == 7, f"inflow nodes: {kinds.count('inflow')}"
    assert kinds.count("outflow") == 7, f"outflow nodes: {kinds.count('outflow')}"
    assert kinds.count("wall") == 14, f"wall nodes: {kinds.count('wall')}"  # 7 top + 7 bottom
    assert {lb.kind for lb in liquids} == {"inflow", "outflow"}

    # default outflow is free / Neumann -> 4 4 4
    cli_path, _ = boundary.write_cli(cfg, mesh)
    cli = Path(cli_path).read_text()
    assert "4 5 5" in cli, "no inflow nodes coded 4 5 5"
    assert "4 4 4" in cli, "free outflow not coded 4 4 4"
    assert "5 4 4" not in cli, "free outflow should not prescribe elevation (5 4 4)"

    # opt-in prescribed-elevation outflow -> 5 4 4
    cfg_elev = load_config(_write_cfg(tmp / "elev", "elevation"))
    cfg_elev.ensure_dirs()
    cli_elev = Path(boundary.write_cli(cfg_elev, mesh)[0]).read_text()
    assert "5 4 4" in cli_elev and "4 4 4" not in cli_elev

    print("BOUNDARY TEST PASSED (classification + outflow codes)")


def test_classification(tmp_path):
    run_boundary_test(tmp_path)


def test_wraparound_not_split(tmp_path):
    """A liquid edge straddling the loop start is ONE boundary, not two.

    Regression for the case where the inflow run wrapped boundary_nodes index 0
    and was emitted as two liquid boundaries (PRESCRIBED FLOWRATES with a spurious
    third value), so TELEMAC - which numbers cyclically (FRONT2) - saw a different
    count and no inflow established."""
    from hydromate import boundary
    from hydromate.config import load_config

    cfg = load_config(_write_cfg(tmp_path, "free"))
    cfg.ensure_dirs()

    # roll the loop so the start falls in the middle of the (inflow) left edge
    mesh = _rect_mesh(n_left=8, n_right=7, roll=-4)
    kinds, liquids = boundary.classify_nodes(cfg, mesh)

    assert kinds.count("inflow") == 8 and kinds.count("outflow") == 7
    kinds_seen = sorted(lb.kind for lb in liquids)
    assert kinds_seen == ["inflow", "outflow"], f"expected 2 boundaries, got {liquids}"
    assert sum(lb.n_nodes for lb in liquids if lb.kind == "inflow") == 8
    # numbering is FRONT2 (SW-most start, domain-on-left): the SW corner sits on
    # the inflow (left) edge, so the inflow boundary is encountered first
    by_index = {lb.index: lb.kind for lb in liquids}
    assert by_index == {1: "inflow", 2: "outflow"}, by_index
    print("WRAP-AROUND TEST PASSED (single boundary, FRONT2 order)")


def test_node_balance_warning(tmp_path, caplog):
    from hydromate import boundary
    from hydromate.config import load_config

    cfg = load_config(_write_cfg(tmp_path, "free"))

    # balanced: no stability warning
    with caplog.at_level(logging.WARNING, logger="hydromate"):
        boundary.classify_nodes(cfg, _rect_mesh(n_left=8, n_right=8))
    assert "STABILITY RISK" not in caplog.text

    # imbalanced (>10%): 9 inflow vs 5 outflow -> warning
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hydromate"):
        boundary.classify_nodes(cfg, _rect_mesh(n_left=9, n_right=5))
    assert "STABILITY RISK" in caplog.text, "expected a node-imbalance warning"
    print("NODE-BALANCE WARNING TEST PASSED")


def test_internal_sources_distributed(tmp_path):
    """Internal losing/gaining lines are distributed across the mesh nodes under
    them (conserving each line's total), and collapse to one midpoint source when
    no mesh is given."""
    from types import SimpleNamespace

    import geopandas as gpd
    import pytest
    from shapely.geometry import LineString

    from hydromate import boundary
    from hydromate.config import load_config

    geo = tmp_path / "geo"
    geo.mkdir(parents=True, exist_ok=True)
    lose = LineString([(X0, Y0), (X0 + 30.0, Y0)])                # 30 m losing line
    gain = LineString([(X0, Y0 + 50.0), (X0 + 45.0, Y0 + 50.0)])  # 45 m gaining line
    gpd.GeoDataFrame(
        {"Type": ["int-outflow-lose", "int-inflow-gain"], "Target flow": [0.065, 0.065]},
        geometry=[lose, gain], crs="EPSG:25832",
    ).to_file(geo / "liquid-boundaries.gpkg", driver="GPKG")
    cfg_yaml = tmp_path / "cfg.yml"
    cfg_yaml.write_text(
        "project:\n  name: t\n  crs_epsg: 25832\n"
        "telemac:\n  pysource: x\n"
        "geodata:\n  dem_initial: geo/dem.tif\n  boundary: geo/boundary.shp\n"
        "boundaries:\n  liquid_boundaries: geo/liquid-boundaries.gpkg\n"
    )
    cfg = load_config(cfg_yaml)

    # a mesh with nodes every 0.5 m along both internal lines
    lose_x = np.arange(0.0, 30.01, 0.5)
    gain_x = np.arange(0.0, 45.01, 0.5)
    mx = np.concatenate([X0 + lose_x, X0 + gain_x])
    my = np.concatenate([np.full(lose_x.size, Y0), np.full(gain_x.size, Y0 + 50.0)])
    mesh = SimpleNamespace(x=mx, y=my)

    srcs = boundary.load_internal_sources(cfg, mesh)
    assert len(srcs) > 10, "expected the exchange distributed over many nodes"
    lose_q = sum(s.discharge for s in srcs if "lose" in s.name)
    gain_q = sum(s.discharge for s in srcs if "gain" in s.name)
    assert lose_q == pytest.approx(-0.065)   # withdrawal, conserved
    assert gain_q == pytest.approx(+0.065)   # injection, conserved
    node_xy = set(zip(mesh.x.tolist(), mesh.y.tolist()))
    assert all((s.x, s.y) in node_xy for s in srcs), "every source sits on a mesh node"

    # no mesh -> legacy single midpoint source per line, net exchange zero
    pts = boundary.load_internal_sources(cfg)
    assert len(pts) == 2
    assert sum(s.discharge for s in pts) == pytest.approx(0.0)
    print("INTERNAL-SOURCE DISTRIBUTION TEST PASSED")


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    run_boundary_test(tmp)
