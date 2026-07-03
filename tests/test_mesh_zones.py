"""Mesh-zone reading: three zone types (channel/floodplain/refinement), the
per-polygon 'Max Edge Length (m)' field (with config fallbacks), decimal-comma
tolerance, and the overlap-priority point assignment. The gmsh mesher is not run.

Run via:
    mamba run -n hydromate-env pytest tests/test_mesh_zones.py
"""

from __future__ import annotations

import math

import numpy as np

from hydromate.mesh import (_assign_point_zones, _classify_zone, _parse_decimal,
                            _read_mesh_zones)


def test_parse_decimal_handles_floats_and_german_commas():
    assert _parse_decimal(0.5) == 0.5
    assert _parse_decimal(2) == 2.0
    assert _parse_decimal("0,5") == 0.5          # German-locale string field
    assert _parse_decimal("2.0") == 2.0
    assert _parse_decimal("") is None
    assert _parse_decimal(None) is None
    assert _parse_decimal(float("nan")) is None


def test_classify_zone_by_substring():
    assert _classify_zone("Channel") == "channel"
    assert _classify_zone("main floodplain") == "floodplain"
    assert _classify_zone("Refinement-bridge") == "refinement"
    assert _classify_zone("weir") == "other"


def _zone_cfg(tmp_path, gpkg):
    from hydromate.config import (
        Boundaries, Calibration, Config, Friction, Geodata, GroundTruth,
        Hydrodynamics, Initialization, MeshConfig, Morphodynamics, TelemacEnv,
    )

    dummy = tmp_path / "dummy"
    dummy.write_text("")
    model = tmp_path / "model"
    model.mkdir()
    return Config(
        name="zones", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path, telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy, mesh_zones=gpkg),
        boundaries=Boundaries(liquid_boundaries=dummy),
        initialization=Initialization(),
        mesh=MeshConfig(floodplain_size=1.5, refinement_size=0.4),
        friction=Friction(), hydrodynamics=Hydrodynamics(),
        morphodynamics=Morphodynamics(), ground_truth=GroundTruth(),
        calibration=Calibration(),
    )


def _box(x0, y0, x1, y1):
    from shapely.geometry import Polygon
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def test_read_zones_uses_field_with_fallback_and_priority(tmp_path):
    import geopandas as gpd

    # floodplain spans the domain; a channel strip and a small refinement box sit on
    # top of it. The refinement polygon has no edge length -> falls back to config.
    zones = gpd.GeoDataFrame(
        {"Zone Name": ["floodplain", "channel", "refinement"],
         "Max Edge Length (m)": [2.0, 0.5, None]},
        geometry=[_box(0, 0, 100, 100), _box(0, 40, 100, 60), _box(45, 45, 55, 55)],
        crs="EPSG:25832",
    )
    gpkg = tmp_path / "mesh-zones.gpkg"
    zones.to_file(gpkg)
    cfg = _zone_cfg(tmp_path, gpkg)

    z = _read_mesh_zones(cfg)
    by_type = dict(zip(z["_zone_type"], z["_edge_length"]))
    assert by_type["floodplain"] == 2.0
    assert by_type["channel"] == 0.5
    assert by_type["refinement"] == 0.4           # fell back to refinement_size

    # a point in floodplain only, in the channel strip, and in the overlap of all
    # three (refinement wins) plus one outside every zone (default floodplain_size)
    pts = np.array([[10.0, 10.0], [10.0, 50.0], [50.0, 50.0], [200.0, 200.0]])
    ztype, edge = _assign_point_zones(cfg, z, pts)
    assert list(ztype) == ["floodplain", "channel", "refinement", "other"]
    assert math.isclose(edge[2], 0.4)             # refinement priority over channel
    assert math.isclose(edge[3], 1.5)             # outside -> default floodplain_size


def test_size_scale_scales_gpkg_and_fallback_sizes(tmp_path):
    """mesh.size_scale must scale the gpkg per-zone sizes too - they override the
    config *_size fallbacks, which is what silently defeated the first ering
    convergence study (five 'levels', one mesh)."""
    import geopandas as gpd

    from hydromate.mesh import nominal_channel_size

    zones = gpd.GeoDataFrame(
        {"Zone Name": ["floodplain", "channel", "refinement"],
         "Max Edge Length (m)": [2.0, 0.5, None]},
        geometry=[_box(0, 0, 100, 100), _box(0, 40, 100, 60), _box(45, 45, 55, 55)],
        crs="EPSG:25832",
    )
    gpkg = tmp_path / "mesh-zones.gpkg"
    zones.to_file(gpkg)
    cfg = _zone_cfg(tmp_path, gpkg)
    cfg.mesh.size_scale = 1.3

    z = _read_mesh_zones(cfg)
    by_type = dict(zip(z["_zone_type"], z["_edge_length"]))
    assert math.isclose(by_type["floodplain"], 2.0 * 1.3)    # gpkg value scaled
    assert math.isclose(by_type["channel"], 0.5 * 1.3)
    assert math.isclose(by_type["refinement"], 0.4 * 1.3)    # config fallback scaled

    _, edge = _assign_point_zones(cfg, z, np.array([[200.0, 200.0]]))
    assert math.isclose(edge[0], 1.5 * 1.3)                  # outside-zones default

    # effective channel size: smallest scaled channel-zone edge length
    assert math.isclose(nominal_channel_size(cfg), 0.5 * 1.3)
    # and the gpkg-free fallback path scales channel_size directly
    cfg.geodata.mesh_zones = None
    assert math.isclose(nominal_channel_size(cfg), cfg.mesh.channel_size * 1.3)
