"""The shared geodata reader (:mod:`axqua.core.geodata`).

Before this existed, fourteen modules each opened their own layers: one build read
``mesh-zones.gpkg`` five times and ``roi.gpkg`` three times, and two modules reached
into ``axqua.mesh._channel_union`` because there was nowhere shared to put it.

The two properties worth pinning are opposites of each other, which is why they are
easy to get wrong together:

* the **raw layer is cached** - that is the whole point, and a regression here is
  invisible except as slowness;
* the **derived values are not** - ``mesh.size_scale`` is varied by the convergence
  study between builds of the same case, so a cached ``_edge_length`` would silently
  hand every level the sizes of the level before it. That is a wrong answer, not a
  crash, so it gets its own test.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from axqua.core import geodata
from axqua.core.geodata import Dataset, classify_zone, dataset, parse_decimal


# --------------------------------------------------------------------------- #
# a tiny synthetic case
# --------------------------------------------------------------------------- #


@pytest.fixture
def case(tmp_path):
    """A minimal case with an ROI, two mesh zones and a centerline."""
    from axqua.config import load_config

    crs = "EPSG:25832"
    gpd.GeoDataFrame(
        {"name": ["roi"]},
        geometry=[Polygon([(0, 0), (100, 0), (100, 40), (0, 40)])], crs=crs,
    ).to_file(tmp_path / "roi.gpkg")
    gpd.GeoDataFrame(
        {"Zone Name": ["Main channel", "Floodplain LB"],
         "Max Edge Length (m)": [0.5, "1,5"]},       # note the German-locale string
        geometry=[Polygon([(0, 10), (100, 10), (100, 30), (0, 30)]),
                  Polygon([(0, 30), (100, 30), (100, 40), (0, 40)])], crs=crs,
    ).to_file(tmp_path / "zones.gpkg")
    gpd.GeoDataFrame(
        {"branch": ["main"]},
        geometry=[LineString([(0, 20), (50, 20), (100, 20)])], crs=crs,
    ).to_file(tmp_path / "centerline.gpkg")
    (tmp_path / "dem.tif").write_bytes(b"")
    (tmp_path / "pysource.sh").write_text("# stub\n")
    (tmp_path / "case-config.yml").write_text("""
project:
  name: geodata-demo
  crs_epsg: 25832
telemac:
  pysource: pysource.sh
geodata:
  dem_initial: dem.tif
  boundary: roi.gpkg
  mesh_zones: zones.gpkg
  channel_centerline: centerline.gpkg
boundaries:
  liquid_boundaries: roi.gpkg
  prescribed_flowrate: 2.0
  prescribed_elevation: 1.0
""")
    return load_config(tmp_path / "case-config.yml")


@pytest.fixture
def counting_reads(monkeypatch):
    """Count every ``gpd.read_file`` by filename."""
    import collections

    counts: collections.Counter = collections.Counter()
    original = gpd.read_file

    def counted(path, *args, **kwargs):
        counts[str(path).rsplit("/", 1)[-1]] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(gpd, "read_file", counted)
    return counts


# --------------------------------------------------------------------------- #
# caching
# --------------------------------------------------------------------------- #


def test_a_layer_is_read_once_however_often_it_is_asked_for(case, counting_reads):
    ds = Dataset(case)
    for _ in range(5):
        ds.mesh_zones()
    for _ in range(4):
        ds.channel_union()
    for _ in range(3):
        ds.roi_polygon()
    assert counting_reads["zones.gpkg"] == 1
    assert counting_reads["roi.gpkg"] == 1


def test_the_dataset_lives_on_the_config(case):
    """Keyed to the config's lifetime, so two configs in one process (the
    convergence study holds several) never share a cache."""
    assert dataset(case) is dataset(case)

    import copy

    other = copy.copy(case)
    assert dataset(other) is not dataset(case)


def test_centerline_tangents_are_cached_per_spacing(case, counting_reads):
    ds = Dataset(case)
    a = ds.centerline_tangents(5.0)
    b = ds.centerline_tangents(5.0)
    c = ds.centerline_tangents(2.0)
    assert a is b                       # same spacing -> same object
    assert c is not a                   # different spacing -> recomputed
    assert counting_reads["centerline.gpkg"] == 1
    points, tangents, tree = a
    assert points.shape[1] == 2 and tangents.shape == points.shape
    assert tree.n == len(points)
    # a straight west-east line: every tangent points along +x
    assert abs(tangents[:, 0]).min() > 0.99


# --------------------------------------------------------------------------- #
# the size_scale trap
# --------------------------------------------------------------------------- #


def test_size_scale_changes_are_honoured_after_the_layer_is_cached(case):
    """The convergence study rebuilds one case at a ladder of size_scale values.
    Caching the derived edge lengths would give every level the previous level's
    mesh - a silent wrong answer, and the reason only raw layers are cached."""
    ds = Dataset(case)
    base = ds.mesh_zones()["_edge_length"].tolist()
    assert base == [0.5, 1.5]                    # incl. the "1,5" German-locale string

    case.mesh.size_scale = 2.0
    scaled = ds.mesh_zones()["_edge_length"].tolist()
    assert scaled == [1.0, 3.0]

    case.mesh.size_scale = 0.5
    assert ds.mesh_zones()["_edge_length"].tolist() == [0.25, 0.75]


def test_zone_defaults_fill_in_for_a_blank_edge_length(case, tmp_path):
    """A zone with no size field falls back to the configured per-type default."""
    gpd.GeoDataFrame(
        {"Zone Name": ["Main channel", "Floodplain"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                  Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])], crs="EPSG:25832",
    ).to_file(tmp_path / "nosize.gpkg")
    case.geodata.mesh_zones = tmp_path / "nosize.gpkg"
    case.mesh.channel_size, case.mesh.floodplain_size = 0.3, 2.0
    assert Dataset(case).mesh_zones()["_edge_length"].tolist() == [0.3, 2.0]


# --------------------------------------------------------------------------- #
# derived geometry
# --------------------------------------------------------------------------- #


def test_channel_union_covers_only_the_channel_zones(case):
    union = Dataset(case).channel_union()
    assert union.area == pytest.approx(100 * 20)     # the channel band only


def test_channel_union_fills_holes(case, tmp_path):
    """A refinement zone carved out of the channel polygon is still channel - it is
    enclosed by channel on every side and merely meshed finer."""
    outer = Polygon([(0, 0), (100, 0), (100, 40), (0, 40)],
                    [[(40, 15), (60, 15), (60, 25), (40, 25)]])
    gpd.GeoDataFrame({"Zone Name": ["Main channel"]}, geometry=[outer],
                     crs="EPSG:25832").to_file(tmp_path / "holed.gpkg")
    case.geodata.mesh_zones = tmp_path / "holed.gpkg"
    assert Dataset(case).channel_union().area == pytest.approx(100 * 40)


def test_a_case_without_a_channel_zone_says_which_zones_it_found(case, tmp_path):
    gpd.GeoDataFrame(
        {"Zone Name": ["Floodplain LB"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])], crs="EPSG:25832",
    ).to_file(tmp_path / "nochannel.gpkg")
    case.geodata.mesh_zones = tmp_path / "nochannel.gpkg"
    with pytest.raises(ValueError, match="floodplain"):
        Dataset(case).channel_union()


def test_roi_polygonises_a_closed_boundary_line(case, tmp_path):
    gpd.GeoDataFrame(
        {"n": [1]},
        geometry=[LineString([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])],
        crs="EPSG:25832",
    ).to_file(tmp_path / "line-roi.gpkg")
    case.geodata.boundary = tmp_path / "line-roi.gpkg"
    assert Dataset(case).roi_polygon().area == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# in-memory layers (the seam a QGIS plugin needs)
# --------------------------------------------------------------------------- #


def test_a_layer_can_be_supplied_in_memory(case, counting_reads):
    """A QGIS plugin has the layers open already; making it write temp files so
    axqua can read them back would be absurd."""
    ds = Dataset(case)
    ds.set_layer("roi", gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])],
        crs="EPSG:25832"))
    assert ds.roi_polygon().area == pytest.approx(25.0)
    assert counting_reads["roi.gpkg"] == 0        # never touched the disk


def test_an_in_memory_layer_is_reprojected_like_a_file(case):
    ds = Dataset(case)
    # EPSG:4326 degrees; after reprojection to 25832 the area is in square metres
    ds.set_layer("roi", gpd.GeoDataFrame(
        {"n": [1]},
        geometry=[Polygon([(11.0, 48.0), (11.001, 48.0), (11.001, 48.001),
                           (11.0, 48.001)])], crs="EPSG:4326"))
    assert ds.roi_polygon().area > 1000.0


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #


def test_parse_decimal_handles_locale_and_blanks():
    assert parse_decimal(0.5) == 0.5
    assert parse_decimal("0,5") == 0.5            # German-locale string field
    assert parse_decimal("2.0") == 2.0
    assert parse_decimal("") is None
    assert parse_decimal(None) is None
    assert parse_decimal(float("nan")) is None
    assert parse_decimal("not a number") is None


def test_classify_zone_by_substring():
    assert classify_zone("Main channel L") == "channel"
    assert classify_zone("Floodplain RB") == "floodplain"
    assert classify_zone("Refinement-bridge") == "refinement"
    assert classify_zone("weir") == "other"


def test_refinement_outranks_channel_outranks_floodplain():
    """Overlap priority: a point in several zones takes the finest intent."""
    order = geodata.ZONE_PRIORITY
    assert order["refinement"] < order["channel"] < order["floodplain"] < order["other"]


def test_roughness_table_skips_a_header_row(tmp_path):
    path = tmp_path / "roughness.csv"
    path.write_text("zone_id,ks\n1,0.2\n2,0.5\n")
    assert geodata.read_roughness_table(path) == {1: 0.2, 2: 0.5}
    headerless = tmp_path / "bare.csv"
    headerless.write_text("1,0.2\n2,0.5\n")
    assert geodata.read_roughness_table(headerless) == {1: 0.2, 2: 0.5}


def test_the_historic_private_names_still_import_from_mesh():
    """Existing code and tests reach for axqua.mesh._parse_decimal and friends;
    the move to core must not break them."""
    from axqua import mesh

    assert mesh._parse_decimal("0,5") == 0.5
    assert mesh._classify_zone("Main channel") == "channel"
    assert mesh._match_field.__name__ == "match_field"
    assert mesh._ZONE_PRIORITY is geodata.ZONE_PRIORITY
