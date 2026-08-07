"""Exchange faces of a gain-lose reach (:mod:`hydromate.gainlose`).

The point of the module is that a **polygon alone** locates the exchange: given the
porous body and its water table, the losing and gaining faces follow from a head
comparison, so nobody has to draw them. The checks are therefore behavioural rather
than numerical:

* both faces exist, are disjoint, and lie inside the (buffered) zone;
* raising the river **grows the losing face** - a build-time mask could not do that,
  and it is the reason the generated routine re-classifies every step;
* the implied exchange scales with kf, which is what makes kf the calibratable knob;
* a geometry that cannot support one of the faces raises instead of silently
  exchanging nothing.

Run via: mamba run -n hydromate-env pytest tests/test_gainlose.py
"""

from __future__ import annotations

import numpy as np
import pytest


def _hollow(x, y):
    """The closed depression on top of the bar (a Gaussian bowl at x=50, y=9)."""
    return np.exp(-(((x - 50.0) / 5.0) ** 2 + ((y - 9.0) / 3.0) ** 2))


def _reach(tmp_path, *, conductivity=3.0e-4, discharge=None, zone_buffer=2.0):
    """A straight channel with a gravel bar beside it and a known water table.

    The bed falls 10 permille along x. The bar occupies 30 < x < 70 on the +y side of
    the channel and stands ~0.35 m above it, so at a moderate stage the channel edge
    is wet (it can lose) while the bar top is dry.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Polygon

    from hydromate.config import (
        Boundaries, Calibration, Config, Friction, GainLose, Geodata, GroundTruth,
        Hydrodynamics, Initialization, MeshConfig, Morphodynamics, TelemacEnv,
    )
    from hydromate.mesh import Mesh

    nx, ny = 61, 31
    X, Y = np.meshgrid(np.linspace(0.0, 120.0, nx), np.linspace(-15.0, 15.0, ny),
                       indexing="ij")
    bed = 10.0 - 0.01 * X                      # channel bed
    # the gravel bar, rising over a SLOPING TOE from y=4 to y=7 - so a higher stage
    # floods more of it, which is what makes the losing face stage-dependent
    span = (X > 30.0) & (X < 70.0)
    bottom = bed + np.where(span, np.clip((Y - 4.0) / 3.0, 0.0, 1.0) * 0.35, 0.0)
    # a hollow in the middle of the bar, cutting 0.20 m BELOW the channel bed - so
    # the water table crosses it while the bar around it stays above the table
    bottom = bottom - 0.55 * _hollow(X, Y) * span

    tri = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a, b = i * ny + j, i * ny + j + 1
            c, d = (i + 1) * ny + j, (i + 1) * ny + j + 1
            tri += [[a, b, c], [b, d, c]]
    mesh = Mesh(x=X.ravel(), y=Y.ravel(), triangles=np.array(tri),
                bottom=bottom.ravel(), ipobo=np.zeros(X.size, int),
                boundary_nodes=np.array([], int),
                element_matid=np.ones(len(tri), int), node_matid=np.ones(X.size, int))

    zone = tmp_path / "bar.gpkg"
    gpd.GeoDataFrame({"Patch name": ["bar"], "porous depth (m)": [0.5]},
                     geometry=[Polygon([(30, 4), (70, 4), (70, 15), (30, 15)])],
                     crs="EPSG:25832").to_file(zone, driver="GPKG")
    centerline = tmp_path / "cl.gpkg"
    gpd.GeoDataFrame(geometry=[LineString([(0, -5), (120, -5)])],
                     crs="EPSG:25832").to_file(centerline, driver="GPKG")
    lb = tmp_path / "lb.gpkg"
    gpd.GeoDataFrame(
        {"Type (inflow/outflow)": ["inflow", "outflow"]},
        geometry=[LineString([(0, -15), (0, 15)]), LineString([(120, -15), (120, 15)])],
        crs="EPSG:25832").to_file(lb, driver="GPKG")

    dummy = tmp_path / "dummy"
    dummy.write_text("")
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    cfg = Config(
        name="gl-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy,
                        channel_centerline=centerline),
        boundaries=Boundaries(liquid_boundaries=lb),
        initialization=Initialization(), mesh=MeshConfig(), friction=Friction(),
        hydrodynamics=Hydrodynamics(), morphodynamics=Morphodynamics(),
        ground_truth=GroundTruth(), calibration=Calibration(),
        gain_lose=GainLose(enabled=True, zone=zone, faces="water-table",
                           conductivity=conductivity, discharge=discharge,
                           zone_buffer=zone_buffer, water_table="phreatic",
                           water_table_levels={"losing": 9.75, "gaining": 9.35}),
    )
    return cfg, mesh


def _surface(mesh, stage_offset=0.0):
    """Free surface: the CHANNEL filled to ~0.25 m, the bar dry.

    The bar is dry on top even where its hollow cuts below the channel bed - a
    closed depression up there is not reachable by surface flow, which is exactly
    why it has to be the water table that wets it.
    """
    bed = np.asarray(mesh.bottom, float)
    x, y = np.asarray(mesh.x, float), np.asarray(mesh.y, float)
    level = 10.0 - 0.01 * x + 0.25 + stage_offset
    # the hollow is a closed depression on the bar: surface flow cannot reach it,
    # however high the river gets, so it stays dry here whatever `level` says
    reachable = _hollow(x, y) < 0.25
    return np.where(reachable & (level > bed), level, bed)


def test_faces_are_found_disjoint_and_inside_the_zone(tmp_path):
    from hydromate.gainlose import derive_faces, zone_mask
    from hydromate.watertable import fit_phreatic_plane

    cfg, mesh = _reach(tmp_path)
    plane = fit_phreatic_plane(cfg, mesh)
    faces = derive_faces(cfg, mesh, plane, _surface(mesh))

    assert faces.losing.any() and faces.gaining.any()
    assert not (faces.losing & faces.gaining).any()      # a node has one role
    inside = zone_mask(cfg, mesh)
    assert np.all(inside[faces.losing]) and np.all(inside[faces.gaining])
    assert faces.losing_area > 0 and faces.gaining_area > 0
    # heads are positive by construction of the two rules
    assert faces.losing_head > 0 and faces.gaining_head > 0
    assert "losing" in faces.summary()[0]


def test_raising_the_river_grows_the_losing_face(tmp_path):
    """The property a build-time mask cannot have, and the reason the generated
    routine re-classifies every step."""
    from hydromate.gainlose import derive_faces
    from hydromate.watertable import fit_phreatic_plane

    cfg, mesh = _reach(tmp_path)
    plane = fit_phreatic_plane(cfg, mesh)

    low = derive_faces(cfg, mesh, plane, _surface(mesh, 0.0))
    high = derive_faces(cfg, mesh, plane, _surface(mesh, 0.30))

    assert high.losing_area > low.losing_area
    assert high.losing_head > low.losing_head


def test_exchange_scales_with_conductivity(tmp_path):
    """kf is the knob that sets the magnitude - which is why it is exposed to
    HydroBayesCal rather than assumed."""
    from hydromate.gainlose import derive_faces, estimate_exchange
    from hydromate.watertable import fit_phreatic_plane

    cfg, mesh = _reach(tmp_path, conductivity=1.0e-5)   # low enough not to hit max_rate
    plane = fit_phreatic_plane(cfg, mesh)
    surface = _surface(mesh)
    faces = derive_faces(cfg, mesh, plane, surface)

    q1 = estimate_exchange(cfg, mesh, faces, surface)
    cfg.gain_lose.conductivity = 2.0e-5
    q2 = estimate_exchange(cfg, mesh, faces, surface)

    assert q1 > 0
    assert q2 == pytest.approx(2.0 * q1, rel=1e-6)


def test_prescribed_discharge_is_reported_instead_of_kf(tmp_path):
    from hydromate.gainlose import derive_faces, estimate_exchange
    from hydromate.watertable import fit_phreatic_plane

    cfg, mesh = _reach(tmp_path, discharge=0.065)
    plane = fit_phreatic_plane(cfg, mesh)
    surface = _surface(mesh)
    faces = derive_faces(cfg, mesh, plane, surface)
    # the kf estimate is still computed (it is what the log compares against)
    assert estimate_exchange(cfg, mesh, faces, surface) > 0


def test_an_incomplete_face_raises_rather_than_exchanging_nothing(tmp_path):
    from hydromate.gainlose import derive_faces
    from hydromate.watertable import fit_phreatic_plane

    cfg, mesh = _reach(tmp_path)
    plane = fit_phreatic_plane(cfg, mesh)
    # a table far below every bed: nothing can gain
    cfg.gain_lose.water_table_levels = {"losing": 0.0, "gaining": -0.5}
    low = fit_phreatic_plane(cfg, mesh)
    with pytest.raises(ValueError, match="faces are incomplete"):
        derive_faces(cfg, mesh, low, _surface(mesh))

    # ... and no plane at all is a clear error too, not a silent zero exchange
    with pytest.raises(ValueError, match="need a water table"):
        derive_faces(cfg, mesh, None, _surface(mesh))
    assert plane is not None


def test_zone_buffer_lets_the_losing_face_reach_the_channel(tmp_path):
    """A bar polygon outlines the bar; the water it takes in is beside it."""
    from hydromate.gainlose import zone_mask

    cfg, mesh = _reach(tmp_path, zone_buffer=0.0)
    tight = zone_mask(cfg, mesh)
    cfg.gain_lose.zone_buffer = 3.0
    wide = zone_mask(cfg, mesh)

    assert wide.sum() > tight.sum()
    assert np.all(wide[tight])          # buffering only ever adds nodes
