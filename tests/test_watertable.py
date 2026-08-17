"""The phreatic water table under a porous gravel bar (:mod:`axqua.watertable`).

The plane is the surface joining the two channel levels the bar exchanges with, so
the checks are: it recovers a known tilted plane from two internal lines, it reads
levels from the seeded surface when no override is given, and - the property that
actually matters in the model - :func:`water_table_depth` is **zero outside the
patch**, because a plane fitted to a bar stands above any lower ground in the reach.

Run via: mamba run -n axqua-env pytest tests/test_watertable.py
"""

from __future__ import annotations

import numpy as np
import pytest


def _case(tmp_path, *, losing_level=10.0, gaining_level=9.0, patch=True,
          faces="lines"):
    """A 100 x 40 m tile with two internal lines 100 m apart, a bar polygon and a
    centerline - enough to fit the water table from either the lines or the zone."""
    import geopandas as gpd
    from shapely.geometry import LineString, Polygon

    from axqua.config import (
        Boundaries, Calibration, Config, Friction, Geodata, GroundTruth,
        Hydrodynamics, Initialization, MeshConfig, Morphodynamics, Percolation,
        TelemacEnv,
    )
    from axqua.mesh import Mesh

    nx, ny = 51, 21
    xs = np.linspace(0.0, 100.0, nx)
    ys = np.linspace(0.0, 40.0, ny)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    # Bed: a uniform 10 permille fall 0.5 m above the table, with a 0.8 m bowl at
    # x=50, y=20 - deep enough to cut 0.3 m below the table, which is the case the
    # whole feature exists for (a closed depression the surface flow cannot reach).
    bottom = (10.5 - 0.01 * X
              - 0.8 * np.exp(-(((X - 50.0) / 4.0) ** 2 + ((Y - 20.0) / 4.0) ** 2)))
    tri = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a, b = i * ny + j, i * ny + j + 1
            c, d = (i + 1) * ny + j, (i + 1) * ny + j + 1
            tri += [[a, b, c], [b, d, c]]
    mesh = Mesh(x=X.ravel(), y=Y.ravel(), triangles=np.array(tri),
                bottom=bottom.ravel(), ipobo=np.zeros(X.size, int),
                boundary_nodes=np.array([], int),
                element_matid=np.ones(len(tri), int),
                node_matid=np.ones(X.size, int))

    lb = tmp_path / "lb.gpkg"
    gpd.GeoDataFrame(
        {"Type (inflow/outflow)": ["int-outflow-lose", "int-inflow-gain",
                                   "inflow", "outflow"],
         "Target flow": [0.065, 0.065, 1.0, 1.0]},
        geometry=[LineString([(0.0, 0.0), (0.0, 40.0)]),
                  LineString([(100.0, 0.0), (100.0, 40.0)]),
                  LineString([(0.0, 40.0), (100.0, 40.0)]),
                  LineString([(0.0, 0.0), (100.0, 0.0)])],
        crs="EPSG:25832").to_file(lb, driver="GPKG")

    zone = tmp_path / "zone.gpkg"
    poly = (Polygon([(30, 5), (70, 5), (70, 35), (30, 35)]) if patch
            else Polygon([(200, 200), (210, 200), (210, 210), (200, 210)]))
    gpd.GeoDataFrame({"Patch name": ["bar"], "porous depth (m)": [0.5]},
                     geometry=[poly], crs="EPSG:25832").to_file(zone, driver="GPKG")

    centerline = tmp_path / "centerline.gpkg"
    gpd.GeoDataFrame(geometry=[LineString([(0.0, 20.0), (100.0, 20.0)])],
                     crs="EPSG:25832").to_file(centerline, driver="GPKG")

    dummy = tmp_path / "dummy"
    dummy.write_text("")
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    cfg = Config(
        name="wt-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy,
                        channel_centerline=centerline),
        boundaries=Boundaries(liquid_boundaries=lb),
        initialization=Initialization(), mesh=MeshConfig(), friction=Friction(),
        hydrodynamics=Hydrodynamics(), morphodynamics=Morphodynamics(),
        ground_truth=GroundTruth(), calibration=Calibration(),
        gain_lose=Percolation(zone=zone, mode="fortran", water_table="phreatic",
                              faces=faces, conductivity=3.0e-4,
                              water_table_levels={"losing": losing_level,
                                                  "gaining": gaining_level}),
    )
    return cfg, mesh


def test_plane_recovers_the_two_line_levels(tmp_path):
    from axqua.watertable import fit_phreatic_plane

    cfg, mesh = _case(tmp_path, losing_level=10.0, gaining_level=9.0)
    plane = fit_phreatic_plane(cfg, mesh)

    assert plane is not None
    # the losing line is at x=0, the gaining line at x=100
    assert plane.elevation(0.0, 20.0) == pytest.approx(10.0, abs=1e-6)
    assert plane.elevation(100.0, 20.0) == pytest.approx(9.0, abs=1e-6)
    assert plane.gradient == pytest.approx(0.01, rel=1e-6)   # 1 m over 100 m
    assert plane.residual < 1e-6                             # exactly co-planar
    assert plane.levels == {"losing": 10.0, "gaining": 9.0}


def test_levels_are_read_from_the_seeded_surface_when_not_configured(tmp_path):
    from axqua.watertable import fit_phreatic_plane

    cfg, mesh = _case(tmp_path)
    cfg.percolation.water_table_levels = None
    # a seeded surface 0.2 m above the bed everywhere
    surface = np.asarray(mesh.bottom, float) + 0.2

    plane = fit_phreatic_plane(cfg, mesh, surface=surface)

    assert plane is not None
    # Bed at x=0 is 10.5 and at x=100 is 9.5 (away from the bowl), so the seeded surface
    # is 10.7 and 9.7 there.
    #
    # The fit lands ~0.02 m low at both ends, and that is a *systematic* offset rather
    # than noise: the plane is least-squares fitted through the whole seeded surface,
    # including the bowl in the middle, which pulls it down slightly at the extremes. The
    # tolerance used to be exactly 0.02, i.e. exactly the size of that offset - so the
    # test passed or failed on the last floating-point bit, and CI duly failed it at
    # 9.720000000000002 where this machine gets 9.719999999999999. 0.05 admits the known
    # offset with room to spare while still catching a genuinely wrong plane, which would
    # be out by at least the 0.2 m seed itself.
    assert plane.elevation(0.0, 2.0) == pytest.approx(10.7, abs=0.05)
    assert plane.elevation(100.0, 2.0) == pytest.approx(9.7, abs=0.05)


def test_depth_is_clipped_to_the_patch(tmp_path):
    """The single property the model depends on: a plane fitted to a bar must not
    wet anything outside it, where it stands above every lower bed in the reach."""
    from axqua.watertable import (
        fit_phreatic_plane, patch_node_mask, water_table_depth,
    )

    cfg, mesh = _case(tmp_path)
    plane = fit_phreatic_plane(cfg, mesh)
    mask = patch_node_mask(cfg, mesh)
    depth = water_table_depth(plane, mesh, mask)

    assert mask.any() and not mask.all()
    assert np.all(depth[~mask] == 0.0)
    # inside the patch it is exactly max(table - bed, 0) ...
    table = plane.elevation(mesh.x, mesh.y)
    assert np.allclose(depth[mask], np.maximum(table - mesh.bottom, 0.0)[mask])
    # ... and the bowl at x=50 is the wet part: table 9.5, bed 9.2 -> ~0.3 m
    assert depth.max() == pytest.approx(0.3, abs=0.02)
    centre = np.argmin(np.hypot(mesh.x - 50.0, mesh.y - 20.0))
    assert depth[centre] == pytest.approx(0.3, abs=0.02)
    # the wet area is the bowl and nothing else - the fall alone never reaches it
    assert 0 < (depth > 0).sum() < 0.1 * mask.sum()


def test_no_plane_without_two_levels(tmp_path, caplog):
    from axqua.watertable import fit_phreatic_plane, water_table_depth

    cfg, mesh = _case(tmp_path)
    cfg.percolation.water_table_levels = {"losing": 10.0}   # gaining missing
    plane = fit_phreatic_plane(cfg, mesh)          # no surface to fall back on
    assert plane is None
    # ... and the depth helper tolerates that rather than exploding
    assert np.all(water_table_depth(None, mesh, np.ones(len(mesh.x), bool)) == 0.0)


def test_plane_from_the_zone_alone_matches_the_line_fit(tmp_path):
    """The whole point of `faces: water-table`: the zone polygon alone locates the
    water table, so nobody has to decide where percolation begins and ends."""
    from axqua.watertable import fit_phreatic_plane

    cfg, mesh = _case(tmp_path, faces="water-table")
    cfg.gain_lose.water_table_levels = None          # derive, do not read off config
    # a channel surface that falls 10 permille along the reach, as the bed does
    surface = 10.6 - 0.01 * np.asarray(mesh.x, float)

    plane = fit_phreatic_plane(cfg, mesh, surface=surface)

    assert plane is not None
    # the zone spans x = 30..70, so the ends are sampled there, not at the domain edge
    assert plane.elevation(30.0, 20.0) == pytest.approx(10.6 - 0.3, abs=0.05)
    assert plane.elevation(70.0, 20.0) == pytest.approx(10.6 - 0.7, abs=0.05)
    assert plane.gradient == pytest.approx(0.01, abs=0.002)
    # the higher end is the one that loses - no digitising direction is assumed
    assert plane.levels["losing"] > plane.levels["gaining"]


def test_zone_fit_needs_a_centerline_and_two_wet_ends(tmp_path):
    """It degrades with a diagnostic rather than inventing a table."""
    from axqua.watertable import fit_phreatic_plane

    cfg, mesh = _case(tmp_path, faces="water-table")
    cfg.gain_lose.water_table_levels = None
    surface = 10.6 - 0.01 * np.asarray(mesh.x, float)

    cfg.geodata.channel_centerline = None
    assert fit_phreatic_plane(cfg, mesh, surface=surface) is None

    cfg, mesh = _case(tmp_path, faces="water-table")
    cfg.gain_lose.water_table_levels = None
    dry = np.asarray(mesh.bottom, float) - 1.0        # nothing is wet anywhere
    assert fit_phreatic_plane(cfg, mesh, surface=dry) is None


def test_patch_mask_empty_without_a_zone(tmp_path):
    from axqua.watertable import patch_node_mask

    cfg, mesh = _case(tmp_path)
    cfg.percolation.zone = None
    assert not patch_node_mask(cfg, mesh).any()
