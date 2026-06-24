"""End-to-end pipeline test on tiny synthetic inputs.

Generates a small ROI (a square channel), breaklines, MATID region points, a
sloping DEM, an inflow series and measurements, then runs the full pipeline and
checks every artifact is produced and structurally sane. Requires the
``telemac-inn`` environment (gmsh, geopandas, rasterio). The TELEMAC solver is
NOT invoked here (``validate_env=False``), so this runs without TELEMAC.

Run directly:  mamba run -n telemac-inn python tests/test_integration.py
Or via pytest: mamba run -n telemac-inn pytest tests/
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def _write_fixtures(d: Path) -> Path:
    import geopandas as gpd
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import LineString, Point, Polygon

    crs = "EPSG:25832"
    # ROI: a 200 m (x) by 60 m (y) rectangle, offset into UTM-32 coordinates
    x0, y0, w, h = 700000.0, 5340000.0, 200.0, 60.0
    geo = d / "geo"
    geo.mkdir(parents=True, exist_ok=True)

    poly = Polygon([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)])
    gpd.GeoDataFrame({"id": [1]}, geometry=[poly], crs=crs).to_file(geo / "boundary.shp")

    # one longitudinal breakline along the channel centreline
    mid = y0 + h / 2
    bl = LineString([(x0 + 5, mid), (x0 + w - 5, mid)])
    gpd.GeoDataFrame({"id": [1]}, geometry=[bl], crs=crs).to_file(geo / "breaklines.shp")

    # liquid boundaries: inflow at the left edge, outflow at the right edge
    inflow = LineString([(x0, y0 + 2), (x0, y0 + h - 2)])
    outflow = LineString([(x0 + w, y0 + 2), (x0 + w, y0 + h - 2)])
    gpd.GeoDataFrame(
        {"id": [1, 2], "type": ["Inflow", "Outflow"]},
        geometry=[inflow, outflow], crs=crs,
    ).to_file(geo / "liquid-boundaries.shp")

    # MATID region seeds: channel (1) vs banks (5)
    seeds = [
        (x0 + w / 2, mid, 1),               # riverbed_fine
        (x0 + w / 2, y0 + 5, 5),            # floodplain
        (x0 + w / 2, y0 + h - 5, 5),
    ]
    gpd.GeoDataFrame(
        {"MATID": [s[2] for s in seeds]},
        geometry=[Point(s[0], s[1]) for s in seeds], crs=crs,
    ).to_file(geo / "region-points.shp")

    # DEM: a plane sloping down-valley (x) with a channel low in the centre
    res = 2.0
    nx, ny = int((w + 20) / res), int((h + 20) / res)
    transform = from_origin(x0 - 10, y0 + h + 10, res, res)  # north-up
    cols = np.arange(nx); rows = np.arange(ny)
    xs = (x0 - 10) + (cols + 0.5) * res
    ys = (y0 + h + 10) - (rows + 0.5) * res
    XX, YY = np.meshgrid(xs, ys)
    z = 380.0 - 0.002 * (XX - x0) - 0.5 * np.exp(-((YY - mid) ** 2) / (2 * 8.0 ** 2))
    with rasterio.open(
        geo / "dem.tif", "w", driver="GTiff", height=ny, width=nx, count=1,
        dtype="float32", crs=crs, transform=transform, nodata=-9999.0,
    ) as dst:
        dst.write(z.astype("float32"), 1)

    # inflow series and measurements
    (geo / "inflow.csv").write_text(
        "datetime,Q\n2020-11-01 00:00,40\n2020-11-01 00:15,42\n2020-11-01 00:30,41\n"
    )
    (geo / "measurements.csv").write_text(
        "id,x,y,water_depth,scalar_velocity\n"
        f"1,{x0 + 50},{mid},0.8,0.6\n"
        f"2,{x0 + 100},{mid},0.7,0.7\n"
        f"3,{x0 + 150},{mid},0.6,0.5\n"
    )

    cfg_yaml = d / "inn-test.yml"
    cfg_yaml.write_text(f"""
project:
  name: inn-test
  crs_epsg: 25832
  work_dir: case
  model_dir: case/sim
  results_dir: case/res
telemac:
  pysource: {geo / "boundary.shp"}   # not sourced in this test (validate_env=False)
  solver: telemac2d
  n_processors: 1
inputs:
  dem_initial: geo/dem.tif
  boundary: geo/boundary.shp
  breaklines: geo/breaklines.shp
  region_points: geo/region-points.shp
  liquid_boundaries: geo/liquid-boundaries.shp
  inflow: geo/inflow.csv
  measurements: geo/measurements.csv
mesh:
  default_size: 6.0
  breakline_size: 3.0
  min_size: 1.0
  region_sizes: {{1: 3.0, 5: 6.0}}
friction:
  zones:
    - {{matid: 1, name: riverbed_fine, law: 4, coefficient: 0.03}}
    - {{matid: 5, name: floodplain,    law: 4, coefficient: 0.06}}
hydrodynamics:
  regime: steady
  n_time_steps: 200
  prescribed_elevation: 379.5
calibration:
  calibration_quantities: ["WATER DEPTH"]
  extraction_quantities: ["WATER DEPTH", "SCALAR VELOCITY"]
  parameters:
    - {{name: zone1, min: 0.02, max: 0.04}}
    - {{name: zone5, min: 0.04, max: 0.09}}
""")
    return cfg_yaml


def run_pipeline_test(tmp: Path) -> None:
    from hydromate.config import load_config
    from hydromate import pipeline

    cfg_yaml = _write_fixtures(tmp)
    cfg = load_config(cfg_yaml)
    art = pipeline.run(cfg, validate_env=False, dry_run=False)

    # every expected artifact exists and is non-empty
    for name in ("geometry_slf", "boundary_cli", "friction_tbl", "cas_file",
                 "calibration_csv", "hbc_config"):
        p = getattr(art, name)
        assert p and Path(p).stat().st_size > 0, f"missing/empty artifact: {name}"

    cli = Path(art.boundary_cli).read_text()
    assert "5 5 5" in cli, "no inflow nodes coded in .cli"
    assert "5 4 4" in cli, "no outflow nodes coded in .cli"

    cas = Path(art.cas_file).read_text()
    assert "PRESCRIBED FLOWRATES" in cas and "FRICTION DATA FILE" in cas

    import pandas as pd
    csv = pd.read_csv(art.calibration_csv)
    assert {"id", "x", "y", "z", "WATER DEPTH_DATA", "WATER DEPTH_ERROR"} <= set(csv.columns)
    assert len(csv) == 3

    tbl = Path(art.friction_tbl).read_text()
    assert tbl.strip().endswith("END") and "MANN" in tbl

    hbc = Path(art.hbc_config).read_text()
    assert "'parameters': ['zone1', 'zone5']" in hbc
    print("INTEGRATION TEST PASSED")
    print(f"  geometry: {art.geometry_slf}")
    print(f"  boundary nodes coded inflow/outflow present in {art.boundary_cli.name}")
    print(f"  calibration points: {len(csv)}")


def test_pipeline(tmp_path):
    run_pipeline_test(tmp_path)


if __name__ == "__main__":
    import tempfile

    run_pipeline_test(Path(tempfile.mkdtemp()))
