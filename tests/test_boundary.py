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
* internal losing/gaining lines become buffered source-region polygons (signed
  discharge, vertex cap, enclosed-node check), and ``percolation.mode: region``
  swaps the losing strip for the percolation patch polygon.

Requires the ``axqua-env`` environment (geopandas). No TELEMAC needed.

Run directly:  mamba run -n axqua-env python tests/test_boundary.py
Or via pytest: mamba run -n axqua-env pytest tests/test_boundary.py
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
    from axqua.mesh import Mesh

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
    from axqua import boundary
    from axqua.config import load_config

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
    from axqua import boundary
    from axqua.config import load_config

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
    from axqua import boundary
    from axqua.config import load_config

    cfg = load_config(_write_cfg(tmp_path, "free"))

    # balanced: no stability warning
    with caplog.at_level(logging.WARNING, logger="axqua"):
        boundary.classify_nodes(cfg, _rect_mesh(n_left=8, n_right=8))
    assert "STABILITY RISK" not in caplog.text

    # imbalanced (>10%): 9 inflow vs 5 outflow -> warning
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="axqua"):
        boundary.classify_nodes(cfg, _rect_mesh(n_left=9, n_right=5))
    assert "STABILITY RISK" in caplog.text, "expected a node-imbalance warning"
    print("NODE-BALANCE WARNING TEST PASSED")


def _internal_lines_cfg(tmp_path, extra_yaml: str = ""):
    """A config whose liquid-boundary layer carries two internal 'int-*' lines."""
    import geopandas as gpd
    from shapely.geometry import LineString

    from axqua.config import load_config

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
        + extra_yaml
    )
    return load_config(cfg_yaml)


def _internal_lines_mesh():
    """Mesh nodes every 0.5 m along both internal lines (plus off-line rows)."""
    from types import SimpleNamespace

    lose_x = np.arange(0.0, 30.01, 0.5)
    gain_x = np.arange(0.0, 45.01, 0.5)
    mx = np.concatenate([X0 + lose_x, X0 + gain_x])
    my = np.concatenate([np.full(lose_x.size, Y0), np.full(gain_x.size, Y0 + 50.0)])
    return SimpleNamespace(x=mx, y=my)


def test_internal_source_regions(tmp_path):
    """Internal losing/gaining lines become buffered source-region polygons with
    signed discharges, a bounded vertex count, and verified enclosed mesh nodes."""
    import pytest

    from axqua import boundary

    cfg = _internal_lines_cfg(tmp_path)
    mesh = _internal_lines_mesh()

    regions = boundary.load_internal_source_regions(cfg, mesh)
    assert len(regions) == 2
    by_name = {("lose" if "lose" in r.name else "gain"): r for r in regions}
    assert by_name["lose"].discharge == pytest.approx(-0.065)   # withdrawal
    assert by_name["gain"].discharge == pytest.approx(+0.065)   # injection
    width = cfg.boundaries.internal_source_region_width
    assert by_name["lose"].area == pytest.approx(30.0 * width, rel=0.05)
    assert by_name["gain"].area == pytest.approx(45.0 * width, rel=0.05)
    for r in regions:
        assert r.n_nodes > 10, "expected the strip to enclose the on-line nodes"
        n_vertices = len(r.polygon.exterior.coords) - 1
        assert n_vertices <= boundary.MAX_REGION_VERTICES

    # no mesh -> regions still built, node counts simply not verified
    bare = boundary.load_internal_source_regions(cfg)
    assert len(bare) == 2 and all(r.n_nodes == 0 for r in bare)

    # a mesh with no node inside a region must raise (TELEMAC aborts on it)
    from types import SimpleNamespace
    far = SimpleNamespace(x=np.array([X0 + 500.0]), y=np.array([Y0 + 500.0]))
    with pytest.raises(ValueError, match="contains no mesh node"):
        boundary.load_internal_source_regions(cfg, far)

    from axqua import steering

    cfg.ensure_dirs()
    text = steering.write_source_regions(cfg, regions).read_text()
    assert "X(1)   Y(1)" in text and "X(2)   Y(2)" in text
    for line in text.splitlines():
        # No blank line, and no leading whitespace, ANYWHERE. TELEMAC's
        # read_source_data.f skips leading spaces with a GO TO that jumps back onto
        # the label that resets the column index, so a whitespace-only line makes
        # telemac2d spin at 100% CPU forever right after "RESCUE : SPALART
        # ALLMARAS" - no error, no time step, no end.
        assert line.strip(), f"blank line in region file -> TELEMAC infinite loop: {text!r}"
        assert not line[0].isspace(), f"leading whitespace -> TELEMAC infinite loop: {line!r}"
        if not line.startswith("#") and not line.startswith("X("):
            # a single "x y" vertex, well under the 72-column DAMOCLES buffer
            assert len(line) < 72 and len(line.split()) == 2

    # MAXSCE counts regions (it sizes PT_IN_POLY as MAXSCE x NPOIN), not their nodes
    cas = steering.write_cas(
        cfg, [], inflow_q=1.0, outflow_wse=None, source_regions=regions,
    ).read_text()
    assert "MAXIMUM NUMBER OF SOURCES : 2" in cas, cas
    # Regions REQUIRE the "normal" source type. With Dirac (2), prosou.f adds the
    # full region discharge at every captured node instead of DSCE/AREA_P, so the
    # exchange is multiplied by the node count (hundreds) - silently.
    assert "TYPE OF SOURCES : 1" in cas, cas
    assert "TYPE OF SOURCES : 2" not in cas, cas
    print("INTERNAL-SOURCE REGION TEST PASSED")


def test_percolation_region_mode(tmp_path):
    """percolation.mode: region spreads the LOSING exchange over the patch polygon
    that intersects the losing line; the gaining line keeps its strip."""
    import geopandas as gpd
    import pytest
    from shapely.geometry import Polygon

    from axqua import boundary

    geo = tmp_path / "geo"
    geo.mkdir(parents=True, exist_ok=True)
    # a 40 x 20 m patch overlapping the losing line (y = Y0)
    patch = Polygon([(X0 - 5, Y0 - 5), (X0 + 35, Y0 - 5),
                     (X0 + 35, Y0 + 15), (X0 - 5, Y0 + 15)])
    gpd.GeoDataFrame(
        {"Patch name": ["main-side"], "porous depth (m)": [0.5]},
        geometry=[patch], crs="EPSG:25832",
    ).to_file(geo / "percolation-zone.gpkg", driver="GPKG")

    # losing_region: patch must be explicit - the default is 'line', because on a
    # patch that is dry on top (water percolating BENEATH it) there is no surface
    # water to withdraw. 'patch' is only meaningful for a SUBMERGED porous zone.
    cfg = _internal_lines_cfg(
        tmp_path,
        "percolation:\n  zone: geo/percolation-zone.gpkg\n  mode: region\n"
        "  losing_region: patch\n")
    regions = boundary.load_internal_source_regions(cfg, _internal_lines_mesh())
    lose = next(r for r in regions if "lose" in r.name)
    gain = next(r for r in regions if "gain" in r.name)
    assert "main-side" in lose.name
    assert lose.area == pytest.approx(patch.area, rel=0.01), \
        "losing region should be the whole percolation patch"
    width = cfg.boundaries.internal_source_region_width
    assert gain.area == pytest.approx(45.0 * width, rel=0.05), \
        "gaining line keeps its buffered strip"
    print("PERCOLATION REGION MODE TEST PASSED")


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    run_boundary_test(tmp)
