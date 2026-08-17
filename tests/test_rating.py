"""Normal-flow stage-discharge generation and the rating-curve reader.

Checks the outflow rating-curve generator (:mod:`axqua.rating`) and the
reader it feeds (:func:`axqua.hydraulics.read_stage_discharge`):

* ``normal_depth`` round-trips — the depth it returns conveys exactly the target
  Q through Manning's equation;
* Manning ``n`` and the equivalent Strickler ``Kst = 1/n`` give the same depth;
* ``generate_stage_discharge`` writes a monotonic ``Q,WSE,depth`` CSV that the
  reader parses, and a single Q-h pair yields a constant (clamped) WSE.

Pure-python (no geopandas/TELEMAC). Run via:
    mamba run -n axqua-env pytest tests/test_rating.py
"""

from __future__ import annotations

import logging
from pathlib import Path

from axqua.hydraulics import read_stage_discharge
from axqua.rating import _conveyance_q, generate_stage_discharge, normal_depth

GEOM = dict(slope=0.001, bottom_width=20.0, side_slope=1.5)


def test_normal_depth_roundtrips():
    for q in (5.0, 25.0, 100.0):
        h = normal_depth(q, manning=0.033, **GEOM)
        back = _conveyance_q(h, 0.033, GEOM["slope"], GEOM["bottom_width"],
                             GEOM["side_slope"])
        assert abs(back - q) < 1e-3, f"Q={q}: depth {h} conveys {back}"


def test_manning_strickler_equivalent():
    h_n = normal_depth(40.0, manning=0.03, **GEOM)
    h_k = normal_depth(40.0, strickler=1 / 0.03, **GEOM)
    assert abs(h_n - h_k) < 1e-6


def test_deeper_for_larger_q():
    shallow = normal_depth(10.0, manning=0.03, **GEOM)
    deep = normal_depth(80.0, manning=0.03, **GEOM)
    assert 0 < shallow < deep


def test_generate_and_read_curve(tmp_path):
    out = generate_stage_discharge(
        tmp_path / "rating.csv", [10, 30, 47, 80],
        manning=0.03, bed_elevation=380.0, **GEOM,
    )
    text = Path(out).read_text().splitlines()
    assert text[0] == "Q,WSE,depth"
    assert len(text) == 5                                  # header + 4 rows

    wse_at = read_stage_discharge(out)
    # interpolates within range, WSE rises with Q, all above the bed
    w30, w47 = wse_at(30), wse_at(47)
    assert 380.0 < w30 < w47
    assert abs(wse_at(47) - 0.5 * (wse_at(40) + wse_at(54))) < 1.0  # ~linear, sane


def test_single_pair_is_constant(tmp_path, caplog):
    out = generate_stage_discharge(tmp_path / "one.csv", 47.0, manning=0.03,
                                   bed_elevation=380.0, **GEOM)
    wse_at = read_stage_discharge(out)
    h47 = wse_at(47.0)
    with caplog.at_level(logging.WARNING, logger="axqua"):
        assert wse_at(60.0) == h47                          # clamped (constant)
    assert "outside the rating curve range" in caplog.text


def test_requires_one_roughness():
    import pytest

    with pytest.raises(ValueError):
        normal_depth(10.0, manning=0.03, strickler=33.0, **GEOM)
    with pytest.raises(ValueError):
        normal_depth(10.0, **GEOM)


def test_synthesize_outflow_rating(tmp_path):
    """Derive an outflow rating from geodata: width from the outflow line, bed +
    slope from the DEM, roughness from friction.boundary_* (Strickler)."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import LineString

    from axqua import synthesize_outflow_rating
    from axqua.config import load_config

    crs = "EPSG:25832"
    x0, y0, w, h = 700000.0, 5340000.0, 200.0, 60.0
    geo = tmp_path / "geo"
    geo.mkdir()
    # DEM sloping down-valley (x): z = 380 - 0.001*(x - x0)
    res = 2.0
    nx, ny = int((w + 20) / res), int((h + 20) / res)
    tr = from_origin(x0 - 10, y0 + h + 10, res, res)
    xs = (x0 - 10) + (np.arange(nx) + 0.5) * res
    z = 380.0 - 0.001 * (np.broadcast_to(xs, (ny, nx)) - x0)
    with rasterio.open(geo / "dem.tif", "w", driver="GTiff", height=ny, width=nx,
                       count=1, dtype="float32", crs=crs, transform=tr,
                       nodata=-9999.0) as d:
        d.write(z.astype("float32"), 1)
    gpd.GeoDataFrame(
        {"Type (inflow/outflow)": ["inflow", "outflow"]},
        geometry=[LineString([(x0, y0 + 2), (x0, y0 + h - 2)]),
                  LineString([(x0 + w, y0 + 2), (x0 + w, y0 + h - 2)])], crs=crs,
    ).to_file(geo / "lb.gpkg", driver="GPKG")
    gpd.GeoDataFrame({"id": [1]}, crs=crs, geometry=[
        LineString([(x0 + 5, y0 + h / 2), (x0 + w - 5, y0 + h / 2)])]
    ).to_file(geo / "cl.gpkg", driver="GPKG")

    (tmp_path / "c.yml").write_text(f"""
project: {{name: t, crs_epsg: 25832}}
telemac: {{pysource: {geo / 'dem.tif'}}}
geodata:
  dem_initial: geo/dem.tif
  boundary: geo/lb.gpkg
  channel_centerline: geo/cl.gpkg
boundaries:
  liquid_boundaries: geo/lb.gpkg
  stage_discharge: geo/rating.csv
friction: {{boundary_law: 3, boundary_coefficient: 38}}
""")
    cfg = load_config(tmp_path / "c.yml")
    out = synthesize_outflow_rating(cfg, 10.0, side_slope=1.0)

    df = pd.read_csv(out)
    assert list(df.columns) == ["Q", "WSE", "depth"]
    assert df["Q"][0] == 10.0
    # bed at the outflow (x = x0+w) ~ 380 - 0.001*200 = 379.8; WSE = bed + depth
    assert df["depth"][0] > 0
    assert 379.8 < df["WSE"][0] < 381.0
    assert abs(df["WSE"][0] - (379.8 + df["depth"][0])) < 0.05


def test_stage_for_discharge_matches_section_rating(tmp_path):
    """``stage_for_discharge`` is the solver ``section_rating`` writes out, so the
    outflow rating and the normal-depth pre-wet cannot drift apart."""
    import numpy as np
    import pandas as pd

    from axqua.rating import section_rating, stage_for_discharge

    station = np.linspace(0.0, 20.0, 81)
    bed = 100.0 + 0.02 * (station - 10.0) ** 2      # V/parabolic section
    ks, slope = 0.05, 0.004
    qs = [1.0, 5.0, 20.0]

    out = section_rating(tmp_path / "r.csv", qs, station=station, bed=bed, ks=ks,
                         slope=slope)
    df = pd.read_csv(out)
    for q, wse in zip(df["Q"], df["WSE"]):
        direct = stage_for_discharge(q, station=station, bed=bed, ks=ks, slope=slope)
        assert abs(direct - wse) < 1e-3


def test_stage_for_discharge_is_monotonic_and_validated():
    import numpy as np
    import pytest

    from axqua.rating import stage_for_discharge

    station = np.linspace(0.0, 20.0, 81)
    bed = 100.0 + 0.02 * (station - 10.0) ** 2
    kw = dict(station=station, bed=bed, ks=0.05, slope=0.004)
    stages = [stage_for_discharge(q, **kw) for q in (1.0, 5.0, 20.0)]
    assert stages[0] < stages[1] < stages[2]
    assert stages[0] > bed.min()                    # above the section thalweg

    with pytest.raises(ValueError, match="positive bed slope"):
        stage_for_discharge(5.0, station=station, bed=bed, ks=0.05, slope=0.0)


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    test_normal_depth_roundtrips()
    test_manning_strickler_equivalent()
    test_generate_and_read_curve(tmp)
    print("RATING TESTS PASSED")
