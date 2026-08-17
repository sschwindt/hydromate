"""Roughness-zone interpolation onto the mesh nodes.

Builds tiny synthetic roughness zones (two side-by-side polygons with integer
``Zone ID`` 1 and 2) plus a zone->ks CSV, then checks:

* :func:`axqua.mesh.read_roughness_table` parses the CSV (with and without a
  header row);
* :func:`axqua.mesh.interpolate_roughness` tags each node with its zone's ks;
* :func:`axqua.mesh.write_mesh` carries the per-node roughness into the
  geometry SELAFIN as the ``BOTTOM FRICTION`` variable.

Requires the ``axqua-env`` environment (geopandas). No TELEMAC needed.

Run directly:  mamba run -n axqua-env python tests/test_roughness.py
Or via pytest: mamba run -n axqua-env pytest tests/test_roughness.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def _write_fixtures(d: Path) -> Path:
    """Two zones split at x = x0 + w/2: left = zone 1 (ks 0.2), right = 2 (0.5)."""
    import geopandas as gpd
    from shapely.geometry import Polygon

    crs = "EPSG:25832"
    x0, y0, w, h = 700000.0, 5340000.0, 200.0, 60.0
    xm = x0 + w / 2
    geo = d / "geo"
    geo.mkdir(parents=True, exist_ok=True)

    left = Polygon([(x0, y0), (xm, y0), (xm, y0 + h), (x0, y0 + h)])
    right = Polygon([(xm, y0), (x0 + w, y0), (x0 + w, y0 + h), (xm, y0 + h)])
    gpd.GeoDataFrame(
        {"Zone ID": [1, 2]}, geometry=[left, right], crs=crs
    ).to_file(geo / "roughness-zones.gpkg", driver="GPKG")

    (geo / "roughness-table.csv").write_text("zone_id,ks\n1,0.2\n2,0.5\n")

    cfg_yaml = d / "rough-test.yml"
    cfg_yaml.write_text(f"""
project:
  name: rough-test
  crs_epsg: 25832
telemac:
  pysource: {geo / "roughness-table.csv"}   # not sourced in this test
geodata:
  dem_initial: geo/dem.tif                   # dummy (existence not checked here)
  boundary: geo/boundary.shp
  roughness_zones: geo/roughness-zones.gpkg
  roughness_table: geo/roughness-table.csv
boundaries:
  liquid_boundaries: geo/lb.shp
""")
    return cfg_yaml


def _toy_mesh(x0=700000.0, y0=5340000.0, w=200.0, h=60.0):
    """A 4-node, 2-triangle mesh straddling the zone split."""
    from axqua.mesh import Mesh

    xm = x0 + w / 2
    # nodes: two left of the split (zone 1), two right (zone 2)
    x = np.array([x0 + w * 0.25, x0 + w * 0.25, xm + w * 0.25, xm + w * 0.25])
    y = np.array([y0 + 10, y0 + h - 10, y0 + 10, y0 + h - 10])
    tris = np.array([[0, 1, 2], [1, 3, 2]])
    return Mesh(
        x=x, y=y, triangles=tris, bottom=np.zeros(4),
        ipobo=np.arange(1, 5), boundary_nodes=np.arange(4),
        element_matid=np.ones(2, dtype=int), node_matid=np.ones(4, dtype=int),
    )


def run_roughness_test(tmp: Path) -> None:
    from axqua.config import load_config
    from axqua.mesh import interpolate_roughness, read_roughness_table, write_mesh

    cfg_yaml = _write_fixtures(tmp)
    cfg = load_config(cfg_yaml)

    # 1) table parsing — header row detected and skipped
    table = read_roughness_table(cfg.geodata.roughness_table)
    assert table == {1: 0.2, 2: 0.5}, f"bad table: {table}"

    # ... and a header-less CSV parses the same
    headerless = tmp / "geo" / "rt-nohdr.csv"
    headerless.write_text("1,0.2\n2,0.5\n")
    assert read_roughness_table(headerless) == {1: 0.2, 2: 0.5}

    # 2) interpolation — left nodes -> zone 1 / ks 0.2, right -> zone 2 / ks 0.5
    mesh = interpolate_roughness(cfg, _toy_mesh())
    assert mesh.roughness is not None
    np.testing.assert_allclose(mesh.roughness, [0.2, 0.2, 0.5, 0.5])
    # FRIC_ID (node_matid) must carry the Zone ID, not stay all-1 (the bug)
    np.testing.assert_array_equal(mesh.node_matid, [1, 1, 2, 2])
    assert set(mesh.element_matid.tolist()) <= {1, 2}

    # 3) the geometry SELAFIN carries BOTTOM FRICTION + the FRIC_ID zones
    slf = write_mesh(mesh, tmp / "mesh.slf", title="rough-test")
    raw = Path(slf).read_bytes()
    assert b"BOTTOM FRICTION" in raw, "roughness not written as BOTTOM FRICTION"
    assert b"FRIC_ID" in raw, "friction zone id not written as FRIC_ID"

    print("ROUGHNESS TEST PASSED")
    print(f"  table: {table}")
    print(f"  per-node ks: {mesh.roughness.tolist()}  FRIC_ID: {mesh.node_matid.tolist()}")
    print(f"  geometry: {slf}")


def test_friction_tbl_from_roughness(tmp_path):
    """The friction .tbl is derived from the roughness table (NIKU + ks) when no
    explicit friction.zones are given, matching the geometry's FRIC_ID zones."""
    from axqua import steering
    from axqua.config import load_config

    cfg = load_config(_write_fixtures(tmp_path))
    cfg.ensure_dirs()
    tbl = Path(steering.write_friction_tbl(cfg)).read_text()
    assert "1\tNIKU\t0.2000\tNULL" in tbl, tbl
    assert "2\tNIKU\t0.5000\tNULL" in tbl, tbl
    assert tbl.strip().endswith("END")
    # the .cas globals reuse the first zone (NIKU law 5)
    assert steering._global_friction(cfg) == (5, 0.2)


def test_roughness(tmp_path):
    run_roughness_test(tmp_path)


if __name__ == "__main__":
    import tempfile

    run_roughness_test(Path(tempfile.mkdtemp()))
