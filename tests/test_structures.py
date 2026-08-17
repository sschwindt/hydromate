"""Dams, weirs, walls and buildings (:mod:`axqua.core.structures`).

The point of this module is that a structure is an **ordinary QGIS vector layer**, not
a triangulated surface. STL is a snappyHexMesh requirement, and axqua writes its
mesh directly, so a dam only has to say where its footprint is and how high it stands.
These tests pin the two authoring forms (polygon footprint, or line + width) and the
two hydraulic modes, because getting the mode wrong is the difference between water
flowing over a weir and being walled off by it.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Polygon

from axqua.core.structures import (
    OVERFLOW, SOLID, Structure, apply_to_bed, classify_structure, load_structures,
    solid_footprint, solid_mask,
)


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def test_types_classify_into_the_two_hydraulic_modes():
    for text in ("Dam", "weir", "Embankment", "levee", "spillway", "block ramp"):
        assert classify_structure(text) == OVERFLOW, text
    for text in ("Wall", "floodwall", "Building 12", "bridge pier", "abutment"):
        assert classify_structure(text) == SOLID, text


def test_block_ramp_is_an_overflow_structure_despite_the_word_block():
    """'block' carries no usable signal: a block ramp is overtopped by design, a
    concrete block is not. The word is therefore not a solid keyword."""
    assert classify_structure("block ramp") == OVERFLOW
    assert classify_structure("Blockrampe") == OVERFLOW


def test_an_unrecognised_type_defaults_to_overflow():
    """Raising the bed is the conservative failure: it keeps the domain connected and
    lets water pass, whereas wrongly blanking a footprint silently walls off a reach."""
    assert classify_structure("groyne-ish thing") == OVERFLOW
    assert classify_structure("") == OVERFLOW


# --------------------------------------------------------------------------- #
# loading, in the two forms QGIS authors
# --------------------------------------------------------------------------- #


def _case(tmp_path, layer_name="structures.gpkg", **structures_cfg):
    from axqua.config import load_config

    (tmp_path / "dem.tif").write_bytes(b"")
    (tmp_path / "pysource.sh").write_text("# stub\n")
    gpd.GeoDataFrame({"n": [1]},
                     geometry=[Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])],
                     crs="EPSG:25832").to_file(tmp_path / "roi.gpkg")
    extra = "".join(f"  {k}: {v}\n" for k, v in structures_cfg.items())
    (tmp_path / "case-config.yml").write_text(f"""
project:
  name: structures-demo
  crs_epsg: 25832
telemac:
  pysource: pysource.sh
geodata:
  dem_initial: dem.tif
  boundary: roi.gpkg
  structures: {layer_name}
boundaries:
  liquid_boundaries: roi.gpkg
  prescribed_flowrate: 2.0
  prescribed_elevation: 1.0
structures:
{extra or "  enabled: true"}
""")
    return load_config(tmp_path / "case-config.yml")


def test_a_polygon_footprint_with_a_crest_elevation(tmp_path):
    gpd.GeoDataFrame(
        {"Name": ["main dam"], "Type": ["dam"], "Crest (m)": [812.5]},
        geometry=[Polygon([(40, 0), (60, 0), (60, 100), (40, 100)])],
        crs="EPSG:25832").to_file(tmp_path / "structures.gpkg")
    structures = load_structures(_case(tmp_path))
    assert len(structures) == 1
    dam = structures[0]
    assert dam.name == "main dam" and dam.mode == OVERFLOW
    assert dam.crest == 812.5 and dam.describes_level_crest
    assert dam.polygon.area == pytest.approx(20 * 100)


def test_a_line_is_buffered_to_its_width(tmp_path):
    """The natural way to draw a wall: trace the crest, say how thick it is."""
    gpd.GeoDataFrame(
        {"Name": ["floodwall"], "Type": ["wall"], "Crest (m)": [815.0],
         "Width (m)": [2.0]},
        geometry=[LineString([(0, 50), (100, 50)])],
        crs="EPSG:25832").to_file(tmp_path / "structures.gpkg")
    wall = load_structures(_case(tmp_path))[0]
    assert wall.mode == SOLID
    # flat caps, so a 100 m line at 2 m wide is a 200 m2 rectangle - not a lozenge
    assert wall.polygon.area == pytest.approx(200.0, rel=1e-6)
    assert wall.polygon.bounds == pytest.approx((0.0, 49.0, 100.0, 51.0))


def test_a_line_without_a_width_uses_the_configured_default(tmp_path):
    gpd.GeoDataFrame(
        {"Type": ["wall"], "Crest (m)": [815.0]},
        geometry=[LineString([(0, 50), (100, 50)])],
        crs="EPSG:25832").to_file(tmp_path / "structures.gpkg")
    wall = load_structures(_case(tmp_path, default_width=4.0))[0]
    assert wall.polygon.area == pytest.approx(400.0, rel=1e-6)


def test_an_explicit_mode_column_overrides_the_type(tmp_path):
    """A dam that is never overtopped is better meshed as solid; the user says so."""
    gpd.GeoDataFrame(
        {"Type": ["dam"], "Mode": ["solid"], "Crest (m)": [900.0]},
        geometry=[Polygon([(40, 0), (60, 0), (60, 100), (40, 100)])],
        crs="EPSG:25832").to_file(tmp_path / "structures.gpkg")
    assert load_structures(_case(tmp_path))[0].mode == SOLID


def test_a_layer_with_neither_crest_nor_height_is_refused(tmp_path):
    gpd.GeoDataFrame(
        {"Type": ["dam"]},
        geometry=[Polygon([(40, 0), (60, 0), (60, 100), (40, 100)])],
        crs="EPSG:25832").to_file(tmp_path / "structures.gpkg")
    with pytest.raises(ValueError, match="crest elevation|neither a crest"):
        load_structures(_case(tmp_path))


def test_the_layer_is_reprojected_like_every_other(tmp_path):
    gpd.GeoDataFrame(
        {"Type": ["dam"], "Crest (m)": [500.0]},
        geometry=[Polygon([(11.0, 48.0), (11.002, 48.0), (11.002, 48.002),
                           (11.0, 48.002)])],
        crs="EPSG:4326").to_file(tmp_path / "structures.gpkg")
    dam = load_structures(_case(tmp_path))[0]
    assert dam.polygon.area > 10_000        # square metres, not square degrees


def test_no_layer_means_no_structures(tmp_path):
    from axqua.config import load_config

    (tmp_path / "dem.tif").write_bytes(b"")
    (tmp_path / "pysource.sh").write_text("# stub\n")
    gpd.GeoDataFrame({"n": [1]}, geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
                     crs="EPSG:25832").to_file(tmp_path / "roi.gpkg")
    (tmp_path / "case-config.yml").write_text("""
project: {name: bare, crs_epsg: 25832}
telemac: {pysource: pysource.sh}
geodata: {dem_initial: dem.tif, boundary: roi.gpkg}
boundaries:
  liquid_boundaries: roi.gpkg
  prescribed_flowrate: 1.0
  prescribed_elevation: 1.0
""")
    assert load_structures(load_config(tmp_path / "case-config.yml")) == []


# --------------------------------------------------------------------------- #
# applying to a mesh
# --------------------------------------------------------------------------- #


def _band(x0, x1):
    return Polygon([(x0, -10), (x1, -10), (x1, 110), (x0, 110)])


def test_an_overflow_crest_raises_the_bed_and_never_lowers_it():
    """A structure adds material; a crest digitised below the surveyed ground must
    not carve a trench through it."""
    xy = np.array([[10.0, 50.0], [50.0, 50.0], [90.0, 50.0]])
    bed = np.array([100.0, 100.0, 100.0])
    tall = Structure("dam", OVERFLOW, _band(40, 60), crest=105.0)
    raised, touched = apply_to_bed([tall], xy, bed)
    assert raised.tolist() == [100.0, 105.0, 100.0]
    assert touched.tolist() == [False, True, False]

    sunken = Structure("low", OVERFLOW, _band(40, 60), crest=95.0)
    kept, _ = apply_to_bed([sunken], xy, bed)
    assert kept.tolist() == [100.0, 100.0, 100.0]


def test_a_height_follows_the_terrain_while_a_crest_is_level():
    """The two ways of saying 'how high' mean different things, which is why both
    exist: a levee is built to a height, a dam is built to a level."""
    xy = np.array([[45.0, 50.0], [55.0, 50.0]])
    sloping_bed = np.array([100.0, 110.0])

    levee = Structure("levee", OVERFLOW, _band(40, 60), height=3.0)
    followed, _ = apply_to_bed([levee], xy, sloping_bed)
    assert followed.tolist() == [103.0, 113.0]          # constant build height

    dam = Structure("dam", OVERFLOW, _band(40, 60), crest=112.0)
    level, _ = apply_to_bed([dam], xy, sloping_bed)
    assert level.tolist() == [112.0, 112.0]             # one crest elevation


def test_solid_structures_do_not_touch_the_bed():
    xy = np.array([[50.0, 50.0]])
    bed = np.array([100.0])
    wall = Structure("wall", SOLID, _band(40, 60), crest=120.0)
    unchanged, touched = apply_to_bed([wall], xy, bed)
    assert unchanged.tolist() == [100.0] and not touched.any()


def test_solid_mask_and_footprint_select_only_solids():
    xy = np.array([[10.0, 50.0], [50.0, 50.0], [90.0, 50.0]])
    dam = Structure("dam", OVERFLOW, _band(0, 20), crest=105.0)
    wall = Structure("wall", SOLID, _band(40, 60), crest=120.0)
    assert solid_mask([dam, wall], xy).tolist() == [False, True, False]
    assert solid_footprint([dam, wall]).area == pytest.approx(_band(40, 60).area)
    assert solid_footprint([dam]) is None


# --------------------------------------------------------------------------- #
# the meshers
# --------------------------------------------------------------------------- #


def test_a_solid_structure_removes_columns_from_the_openfoam_lattice():
    """And the removal must survive the hole-filling step, which exists to close
    exactly this kind of interior void."""
    from axqua.solvers.openfoam.mesh import build_plan_grid

    domain = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
    plain = build_plan_grid(domain, 1.0)
    blocked = build_plan_grid(domain, 1.0, blocked=Polygon([(10, 10), (20, 10),
                                                            (20, 20), (10, 20)]))
    assert blocked.n_columns < plain.n_columns
    assert plain.n_columns - blocked.n_columns == pytest.approx(100, abs=25)
    # the void is genuinely absent, not filled back in
    inside = ((blocked.cell_xy[:, 0] > 11) & (blocked.cell_xy[:, 0] < 19)
              & (blocked.cell_xy[:, 1] > 11) & (blocked.cell_xy[:, 1] < 19))
    assert not inside.any()


def test_a_wall_across_the_whole_domain_is_reported_not_silently_halved(caplog):
    """Cutting the domain in two is almost always a drawing error; it must be loud."""
    import logging

    from axqua.solvers.openfoam.mesh import build_plan_grid

    domain = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
    with caplog.at_level(logging.WARNING, logger="axqua"):
        grid = build_plan_grid(domain, 1.0,
                               blocked=Polygon([(14, -1), (16, -1), (16, 31), (14, 31)]))
    assert "cut the domain in two" in caplog.text
    assert grid.n_columns < 30 * 30 / 2 + 30      # only one side survived
