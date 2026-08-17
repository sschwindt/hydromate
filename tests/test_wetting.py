"""Wetted-extent diagnostics (:mod:`axqua.wetting`).

Both checks run on synthetic SELAFIN results written by axqua's own writer, so
no solver is involved:

* :func:`axqua.wetting.wetting_report` splits a constructed result into a
  flowing channel, a stagnant film and a disconnected puddle, and must recover each
  area/volume, count the puddle as isolated, and attribute the film to the seed;
* :func:`axqua.wetting.outlet_profile` must call a surface that flattens into
  the boundary *backwater*, one that steepens into it *drawdown*, and one that
  continues the reach slope *neutral*.

Run via: mamba run -n axqua-env pytest tests/test_wetting.py
"""

from __future__ import annotations

import numpy as np
import pytest

from axqua import selafin

NC = 21           # nodes across
NS = 41           # nodes along
DX = 2.0          # node spacing [m]


def _grid():
    """Regular triangulated grid: coordinates, element table (1-based), ipobo."""
    s = np.arange(NS) * DX
    c = (np.arange(NC) - NC // 2) * DX
    S, C = np.meshgrid(s, c, indexing="ij")
    tri = []
    for i in range(NS - 1):
        for j in range(NC - 1):
            a, b = i * NC + j, i * NC + j + 1
            d, e = (i + 1) * NC + j, (i + 1) * NC + j + 1
            tri += [[a, b, d], [b, e, d]]
    return S.ravel(), C.ravel(), np.array(tri) + 1, C.ravel()


def _write_result(path, *, depth, u, v, bottom, times=(0.0,), grow=None):
    """Write a multi-frame result SELAFIN with the variables the report reads."""
    x, y, ikle, _ = _grid()
    frames = []
    for k, t in enumerate(times):
        d = depth if grow is None else grow(depth, k)
        frames.append((t, [("VELOCITY U", "M/S", u), ("VELOCITY V", "M/S", v),
                           ("WATER DEPTH", "M", d), ("FREE SURFACE", "M", bottom + d),
                           ("BOTTOM", "M", bottom)]))
    return _write_frames(path, x, y, ikle, frames)


def _write_frames(path, x, y, ikle, frames):
    """Multi-frame SELAFIN via the single-frame writer plus appended frames."""
    import struct

    ipobo = np.zeros(x.size, dtype=int)
    first_t, first_vars = frames[0]
    selafin._write_selafin(path, x, y, ikle, ipobo, first_vars,
                           title="wetting test", date=(2026, 1, 1, 0, 0, 0),
                           double_precision=False)
    with open(path, "ab") as fh:
        for t, variables in frames[1:]:
            rec = struct.pack(">f", t)
            fh.write(struct.pack(">i", len(rec)) + rec + struct.pack(">i", len(rec)))
            for _, _, values in variables:
                data = np.asarray(values, dtype=">f4").tobytes()
                fh.write(struct.pack(">i", len(data)) + data
                         + struct.pack(">i", len(data)))
    return path


def _scene():
    """A channel carrying flow, a stagnant film beside it and an isolated puddle."""
    x, y, ikle, cross = _grid()
    bottom = np.zeros(x.size)
    depth = np.zeros(x.size)
    u = np.zeros(x.size)
    v = np.zeros(x.size)

    channel = np.abs(cross) <= 4.0            # 5 columns: the flowing thread
    depth[channel] = 0.40
    u[channel] = 0.80

    film = (cross > 4.0) & (cross <= 12.0)    # contiguous with the channel, immobile
    depth[film] = 0.03
    u[film] = 0.005

    # a puddle detached from both (a dry gap at cross = -6 .. -8 separates it)
    puddle = (cross <= -10.0) & (x >= 20.0) & (x <= 40.0)
    depth[puddle] = 0.20
    return x, y, ikle, bottom, depth, u, v, channel, film, puddle


def _areas(mask):
    """Nodal area of *mask* on the regular grid (matches wetting._nodal_areas)."""
    from axqua.wetting import _nodal_areas

    x, y, ikle, _ = _grid()
    return _nodal_areas(x, y, ikle - 1)[mask].sum()


def test_wetting_report_splits_active_film_and_puddle(tmp_path):
    from axqua.wetting import wetting_report

    x, y, ikle, bottom, depth, u, v, channel, film, puddle = _scene()
    res = _write_result(tmp_path / "r2d.slf", depth=depth, u=u, v=v, bottom=bottom)

    rep = wetting_report(res, history_frames=0)

    assert rep.wet_area == pytest.approx(_areas(depth > 0.01), rel=1e-6)
    assert rep.active_area == pytest.approx(_areas(channel), rel=1e-6)
    assert rep.film_area == pytest.approx(_areas(film | puddle), rel=1e-6)
    assert rep.active_volume == pytest.approx(0.40 * _areas(channel), rel=1e-6)
    # the puddle is the only body not connected to the channel
    assert rep.isolated_count == 1
    assert rep.isolated_area == pytest.approx(_areas(puddle), rel=1e-6)
    assert 0.0 < rep.film_fraction < 1.0
    assert rep.seeded_area is None          # no initial conditions given


def test_wetting_report_attributes_the_film_to_the_seed(tmp_path):
    from axqua.wetting import wetting_report

    x, y, ikle, bottom, depth, u, v, channel, film, puddle = _scene()
    res = _write_result(tmp_path / "r2d.slf", depth=depth, u=u, v=v, bottom=bottom)
    # the seed wetted the channel and the film strip, but not the puddle
    seed = np.where(channel | film, 0.5, 0.0)
    ic = selafin.write_initial_state(tmp_path / "ic.slf", x, y, ikle,
                                     np.zeros(x.size, int), seed)

    rep = wetting_report(res, initial_conditions=ic, history_frames=0)

    assert rep.seeded_area == pytest.approx(_areas(channel | film), rel=1e-6)
    assert rep.film_seeded_area == pytest.approx(_areas(film), rel=1e-6)
    # the puddle is film the flow put there, the strip is film the seed put there
    assert rep.film_seeded_fraction == pytest.approx(
        _areas(film) / _areas(film | puddle), rel=1e-6)


def test_wetting_report_film_trend_flags_a_plateau(tmp_path):
    from axqua.wetting import wetting_report

    x, y, ikle, bottom, depth, u, v, channel, film, puddle = _scene()
    # frames where the film does not change: the plateau the isar-2025 runs showed
    res = _write_result(tmp_path / "flat.slf", depth=depth, u=u, v=v, bottom=bottom,
                        times=(0.0, 100.0, 200.0, 300.0))
    rep = wetting_report(res, history_frames=4)
    assert len(rep.film_history) == 4
    assert rep.film_trend == pytest.approx(0.0, abs=1e-9)
    assert "PLATEAUED" in "\n".join(rep.summary())

    # ... against one where the film is draining away frame by frame
    def shrink(d, k):
        out = d.copy()
        out[film] = max(0.0, 0.03 - 0.01 * k)
        return out

    res2 = _write_result(tmp_path / "drain.slf", depth=depth, u=u, v=v, bottom=bottom,
                         times=(0.0, 100.0, 200.0, 300.0), grow=shrink)
    rep2 = wetting_report(res2, history_frames=4)
    assert rep2.film_trend < -0.5
    assert "still draining" in "\n".join(rep2.summary())


def test_supported_water_is_not_counted_as_film_or_puddle(tmp_path):
    """Water the bar's water table holds in place is legitimately wet. Without the
    mask it reads as BOTH stagnant film and an isolated puddle - i.e. as a defect,
    when it is exactly what the model intends."""
    from axqua.wetting import wetting_report

    x, y, ikle, bottom, depth, u, v, channel, film, puddle = _scene()
    res = _write_result(tmp_path / "r2d.slf", depth=depth, u=u, v=v, bottom=bottom)

    plain = wetting_report(res, history_frames=0)
    held = wetting_report(res, supported=puddle.astype(float) * 0.2,
                          history_frames=0)

    # the puddle moves out of the defect categories and into its own
    assert plain.isolated_area == pytest.approx(_areas(puddle), rel=1e-6)
    assert held.isolated_area == pytest.approx(0.0, abs=1e-9)
    assert held.isolated_count == 0
    assert held.film_area == pytest.approx(_areas(film), rel=1e-6)
    assert held.supported_area == pytest.approx(_areas(puddle), rel=1e-6)
    # the wetted total is unchanged - nothing was hidden, only reclassified
    assert held.wet_area == pytest.approx(plain.wet_area, rel=1e-9)
    assert "water-table pools" in "\n".join(held.summary())


def test_supported_mask_of_the_wrong_size_is_ignored(tmp_path):
    from axqua.wetting import wetting_report

    x, y, ikle, bottom, depth, u, v, channel, film, puddle = _scene()
    res = _write_result(tmp_path / "r2d.slf", depth=depth, u=u, v=v, bottom=bottom)
    rep = wetting_report(res, supported=np.ones(5, dtype=bool), history_frames=0)
    assert rep.supported_area == 0.0


def _outlet_cfg(tmp_path):
    """A config whose outflow line sits at the downstream end of the test grid."""
    import geopandas as gpd
    from shapely.geometry import LineString

    from axqua.config import (
        Boundaries, Calibration, Config, Friction, Geodata, GroundTruth,
        Hydrodynamics, Initialization, MeshConfig, Morphodynamics, TelemacEnv,
    )

    lb = tmp_path / "lb.gpkg"
    gpd.GeoDataFrame(
        {"Type (inflow/outflow)": ["outflow", "inflow"]},
        geometry=[LineString([(0.0, -20.0), (0.0, 20.0)]),
                  LineString([((NS - 1) * DX, -20.0), ((NS - 1) * DX, 20.0)])],
        crs="EPSG:25832").to_file(lb, driver="GPKG")
    dummy = tmp_path / "dummy"
    dummy.write_text("")
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    return Config(
        name="outlet-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy),
        boundaries=Boundaries(liquid_boundaries=lb),
        initialization=Initialization(), mesh=MeshConfig(), friction=Friction(),
        hydrodynamics=Hydrodynamics(), morphodynamics=Morphodynamics(),
        ground_truth=GroundTruth(), calibration=Calibration(),
    )


def _profile_result(tmp_path, name, surface_of):
    """A flowing channel whose free surface follows *surface_of(distance)*."""
    x, y, ikle, cross = _grid()
    channel = np.abs(cross) <= 6.0
    surface = surface_of(x)
    bottom = np.where(channel, surface - 0.4, surface + 1.0)
    depth = np.where(channel, 0.4, 0.0)
    u = np.where(channel, 0.8, 0.0)
    v = np.zeros(x.size)
    return _write_result(tmp_path / name, depth=depth, u=u, v=v, bottom=bottom)


# The surface RISES with distance from the outflow line (the water runs downhill
# towards it); the reach above 10 m always falls at 5 permille towards the boundary.
def _reach(d, near_slope, near=10.0):
    return np.where(d <= near, 100.0 + near_slope * d,
                    100.0 + near_slope * near + 0.005 * (d - near))


@pytest.mark.parametrize("name,surface,expected", [
    # the last 10 m flattens to 1 permille: the boundary is holding the level up
    ("backwater", lambda d: _reach(d, 0.001), "backwater"),
    # ... steepens to 20 permille: the flow is pulled down into the boundary
    ("drawdown", lambda d: _reach(d, 0.020), "drawdown"),
    # ... continues the reach slope unchanged
    ("neutral", lambda d: _reach(d, 0.005), "neutral"),
])
def test_outlet_profile_verdicts(tmp_path, name, surface, expected):
    from axqua.wetting import outlet_profile

    cfg = _outlet_cfg(tmp_path)
    res = _profile_result(tmp_path, f"{name}.slf", surface)
    prof = outlet_profile(cfg, res)

    assert prof.verdict == expected
    # bands are ordered outward from the boundary and the surface rises upstream
    assert [b["from_m"] for b in prof.bands] == sorted(b["from_m"] for b in prof.bands)
    assert prof.bands[-1]["wse"] > prof.bands[0]["wse"]


def test_outlet_profile_writes_csv(tmp_path):
    from axqua.wetting import outlet_profile

    cfg = _outlet_cfg(tmp_path)
    res = _profile_result(tmp_path, "n.slf", lambda d: _reach(d, 0.005))
    outlet_profile(cfg, res, out=tmp_path)

    text = (tmp_path / "outlet-profile.csv").read_text()
    assert text.startswith("# verdict")
    assert "froude" in text.splitlines()[1]
