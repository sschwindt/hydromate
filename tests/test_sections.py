"""Cross-section discharge extraction, spill elevations and the section rating.

Three pieces added after the 2026-08-02 review of the isar-2025 results, none of
which involve the TELEMAC solver:

* :func:`axqua.sections.line_discharges` - the discharge across a GIS line,
  integrated from a result SELAFIN (the "baffle" cross-sections);
* :func:`axqua.steering.spill_elevations` - the priority-flood sweep behind
  drainable pre-wetting;
* :func:`axqua.rating.section_rating` - stage-discharge from a surveyed
  cross-section instead of a trapezoid.
"""

from __future__ import annotations

import csv

import numpy as np
import pytest

from axqua import selafin


def _uniform_channel(nx=21, ny=15, length=20.0, width=14.0, depth=0.5, u=1.0):
    """A rectangular grid carrying a uniform flow of ``depth * width * u`` m3/s.

    The flow fills the mesh edge to edge, so a section spanning exactly ``0..width``
    integrates the analytic discharge with no discretisation edge effect. (A dry bank
    would not work as a check: with P1 fields the depth ramps linearly across the
    bank cell, so the discretised field genuinely carries more than a sharp-edged
    channel of the same nominal width.)
    """
    xs = np.linspace(0.0, length, nx)
    ys = np.linspace(0.0, width, ny)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    x, y = gx.ravel(), gy.ravel()
    tri = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a = i * ny + j
            b = a + 1
            c = a + ny
            d = c + 1
            tri.append([a, c, b])
            tri.append([b, c, d])
    ikle = np.array(tri) + 1
    n = x.size
    return x, y, ikle, np.full(n, depth), np.full(n, u), np.zeros(n)


def test_line_discharge_recovers_uniform_flow(tmp_path):
    """A section across a uniform flow returns width * depth * velocity, whichever
    way the line is digitised, and an oblique cut of the same flow gives the same
    discharge (only the normal component counts)."""
    pytest.importorskip("matplotlib")
    import geopandas as gpd
    from shapely.geometry import LineString

    from axqua.sections import line_discharges

    depth, speed, width = 0.5, 1.2, 14.0
    x, y, ikle, h, u, v = _uniform_channel(depth=depth, u=speed, width=width)
    res = tmp_path / "r2d.slf"
    selafin.write_initial_state(res, x, y, ikle, np.zeros(x.size, dtype=int), h,
                                velocity_u=u, velocity_v=v)
    expected = depth * width * speed          # 8.4 m3/s

    lines = gpd.GeoDataFrame(
        {"name": ["straight", "reversed", "oblique"]},
        geometry=[
            LineString([(10.0, 0.0), (10.0, 14.0)]),       # across the flow
            LineString([(10.0, 14.0), (10.0, 0.0)]),       # same, digitised backwards
            LineString([(4.0, 0.0), (16.0, 14.0)]),        # oblique cut
        ], crs="EPSG:25832")
    df = line_discharges(res, lines, name_field="name", crs_epsg=25832).set_index("name")

    assert df.loc["straight", "discharge"] == pytest.approx(expected, rel=2e-3)
    # digitising direction must not change the answer (the sign is normalised)
    assert df.loc["reversed", "discharge"] == pytest.approx(expected, rel=2e-3)
    # an oblique section carries the same discharge: the extra length is exactly
    # compensated by the smaller normal component
    assert df.loc["oblique", "discharge"] == pytest.approx(expected, rel=2e-3)
    assert df.loc["straight", "wetted_width"] == pytest.approx(width, abs=0.3)
    assert df.loc["straight", "mean_depth"] == pytest.approx(depth, rel=1e-3)
    assert df.loc["straight", "mean_velocity"] == pytest.approx(speed, rel=5e-3)


def test_line_discharge_rejects_mismatched_geometry(tmp_path):
    """A result from a different build must fail loudly, not be silently sampled."""
    pytest.importorskip("matplotlib")
    import geopandas as gpd
    from shapely.geometry import LineString

    from axqua.sections import line_discharges

    x, y, ikle, h, *_ = _uniform_channel(nx=21, ny=11)
    res = tmp_path / "r2d.slf"
    selafin.write_initial_state(res, x, y, ikle, np.zeros(x.size, dtype=int), h)

    x2, y2, ikle2, h2, *_ = _uniform_channel(nx=15, ny=9)   # a different mesh
    geo = tmp_path / "geometry.slf"
    selafin.write_initial_state(geo, x2, y2, ikle2, np.zeros(x2.size, dtype=int), h2)

    lines = gpd.GeoDataFrame({"name": ["mid"]},
                             geometry=[LineString([(10.0, -1.0), (10.0, 11.0)])],
                             crs="EPSG:25832")
    with pytest.raises(ValueError, match="different builds"):
        line_discharges(res, lines, geometry=geo, name_field="name", crs_epsg=25832)


class _Mesh:
    def __init__(self, x, y, triangles, bottom, ipobo):
        self.x, self.y = x, y
        self.triangles, self.bottom, self.ipobo = triangles, bottom, ipobo


def test_spill_elevation_finds_the_rim_of_a_closed_pit():
    """A pit surrounded by higher ground spills at the lowest rim, not at its bed."""
    from axqua.steering import spill_elevations

    x, y, ikle, *_ = _uniform_channel(nx=21, ny=11)
    tri = ikle - 1
    bottom = np.full(x.size, 10.0)
    # a saddle: the ground dips to 9.5 along one edge of the grid (the outlet side)
    bottom[x < 0.5] = 9.0
    # a closed pit in the middle, bed 8.0, rim 10.0
    pit = (np.abs(x - 10.0) < 1.5) & (np.abs(y - 5.0) < 1.5)
    bottom[pit] = 8.0

    outlet = x < 1e-9
    spill = spill_elevations(_Mesh(x, y, tri, bottom, outlet.astype(int)), outlet)

    assert np.allclose(spill[outlet], bottom[outlet])          # outlets spill at bed
    # freely draining ground away from the pit: spill == bed
    plain = (~pit) & (x > 2.0)
    assert np.allclose(spill[plain], bottom[plain])
    # inside the pit the spill is the surrounding rim, well above the pit bed
    assert np.all(spill[pit] > bottom[pit] + 1.5)
    assert spill[pit].max() == pytest.approx(10.0, abs=1e-6)


def test_section_rating_beats_the_trapezoid_on_a_v_shaped_section(tmp_path):
    """On a V-shaped section the trapezoid over-states the area and so returns too
    low a stage - the mechanism behind the supercritical outlet drawdown."""
    from axqua.rating import normal_depth, section_rating

    width, slope, ks = 10.0, 0.004, 0.1
    station = np.linspace(0.0, width, 201)
    bed = 100.0 + 0.4 * np.abs(station - width / 2) / (width / 2)   # V, 0.4 m deep

    out = section_rating(tmp_path / "rating.csv", 2.4, station=station,
                         bed=bed, ks=ks, slope=slope)
    with open(out) as fh:
        row = next(iter(csv.DictReader(fh)))
    wse_section = float(row["WSE"])

    # the trapezoid the old method would have used: full width at the full depth
    h_trap = normal_depth(2.4, strickler=1.0 / ((ks ** (1 / 6)) * 0.0474 / 1.0),
                          slope=slope, bottom_width=width)
    wse_trap = float(bed.min()) + h_trap

    assert wse_section > wse_trap, (wse_section, wse_trap)
    # and the section actually conveys the discharge at that stage
    h = np.maximum(wse_section - bed, 0.0)
    assert (h > 0).any() and h.max() < 2.0
