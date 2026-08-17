"""One reader for a case's geodata layers.

Before this module, every part of axqua that needed a layer opened it itself:
fourteen modules each doing their own ``gpd.read_file`` plus a CRS reprojection, with
no caching. ``_read_mesh_zones`` alone was called from five places in a single build,
re-reading the same GeoPackage every time, and two modules reached into another
module's private ``_channel_union`` because there was nowhere shared to put it. This
is that shared place.

What is cached, and what deliberately is not
--------------------------------------------
Only the **raw layer** is cached - the expensive part, the disk read and the
reprojection. Everything derived from it is recomputed per call, because derived
values depend on configuration that legitimately changes during a session: the
mesh-convergence study rebuilds the same case at a ladder of ``mesh.size_scale``
values, and caching the *scaled* zone edge lengths would hand every level after the
first the sizes of the level before it. That is a silent wrong-answer bug rather than
a crash, so the split is enforced by construction: :meth:`Dataset.mesh_zones` derives
its columns fresh each time from a cached frame.

Layer sources
-------------
A layer may be given as a path (the normal case) or handed over **already loaded**
via :meth:`Dataset.set_layer`. The second form exists because a QGIS plugin has the
layers open in the project already, and forcing it to write temporary GeoPackages so
axqua can read them back would be absurd.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("axqua")

# overlap priority: a point inside several zones takes the finest intent
ZONE_PRIORITY = {"refinement": 0, "channel": 1, "floodplain": 2, "other": 3}


# --------------------------------------------------------------------------- #
# small parsing helpers, shared by every layer reader
# --------------------------------------------------------------------------- #


def match_field(gdf, name: str) -> str | None:
    """Case-insensitive column lookup (so 'Zone Name'/'zone name' both match)."""
    return next((c for c in gdf.columns if str(c).lower() == name.lower()), None)


def parse_decimal(value) -> float | None:
    """Parse a numeric attribute robustly.

    A GeoPackage ``double`` field already comes back as a float, but a field authored
    in a German-locale GIS may arrive as the *string* ``"0,5"``. Accept both:
    floats/ints pass through; strings have their decimal comma normalised to a point
    before parsing. Returns ``None`` for blanks / NaN / unparseable values so the
    caller can fall back to a configured default.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    text = str(value).strip().replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def classify_zone(name: str) -> str:
    """Map a 'Zone Name' to a zone type by substring (case-insensitive)."""
    lowered = str(name).lower()
    if "channel" in lowered:
        return "channel"
    if "refinement" in lowered:        # local refinement zones (isotropic, fine)
        return "refinement"
    if "floodplain" in lowered:
        return "floodplain"
    return "other"


def fill_holes(geom):
    """Return *geom* with all interior rings removed (only the exterior kept).

    A mesh-zone author commonly carves holes into the channel polygon so a finer
    ``refinement`` zone can be nested inside (e.g. around a structure). Such a hole is
    enclosed by channel on every side, so it *is* channel - just meshed finer.
    Dropping the holes keeps those nested pockets part of the channel footprint so
    they are pre-wetted with the rest of the channel.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    if geom.geom_type == "Polygon":
        return Polygon(geom.exterior)
    if geom.geom_type == "MultiPolygon":
        return unary_union([Polygon(p.exterior) for p in geom.geoms])
    return geom


# --------------------------------------------------------------------------- #
# the dataset
# --------------------------------------------------------------------------- #


class Dataset:
    """A case's geodata layers, read once and reprojected to the project CRS."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.crs_epsg = int(cfg.crs_epsg)
        self._layers: dict[str, Any] = {}      # role -> GeoDataFrame
        self._derived: dict[str, Any] = {}     # cheap-but-not-free geometry results

    # ------------------------------------------------------------- layer I/O
    def set_layer(self, role: str, frame) -> None:
        """Supply a layer directly instead of reading it from disk.

        The seam a QGIS plugin needs: it already has the layers open, and should not
        have to round-trip them through temporary files. Also useful in tests.
        """
        self._layers[role] = self._to_crs(frame)
        self._derived.clear()

    def _to_crs(self, gdf):
        if gdf.crs is not None and gdf.crs.to_epsg() != self.crs_epsg:
            return gdf.to_crs(epsg=self.crs_epsg)
        return gdf

    def layer(self, role: str, path=None):
        """The raw layer for *role*, read at most once per :class:`Dataset`.

        Only this - the disk read and the reprojection - is cached. Anything derived
        from a layer is recomputed by its caller, because it may depend on config
        that changes between calls (see the module docstring).
        """
        if role in self._layers:
            return self._layers[role]
        if path is None:
            raise ValueError(f"no source configured for the {role!r} layer")
        import geopandas as gpd

        log.debug("reading %s layer: %s", role, path)
        self._layers[role] = self._to_crs(gpd.read_file(Path(path)))
        return self._layers[role]

    def has(self, role: str, path=None) -> bool:
        return role in self._layers or path is not None

    # ------------------------------------------------------------------- ROI
    def roi_polygon(self):
        """The region of interest as a shapely Polygon (polygonising lines if needed)."""
        if "roi" not in self._derived:
            from shapely.ops import polygonize, unary_union

            gdf = self.layer("roi", self.cfg.geodata.boundary)
            geoms = list(gdf.geometry.values)
            if set(gdf.geom_type) & {"LineString", "MultiLineString"}:
                polys = list(polygonize(unary_union(geoms)))
                if not polys:
                    raise ValueError("boundary lines do not close into a polygon")
                self._derived["roi"] = max(polys, key=lambda p: p.area)
            else:
                self._derived["roi"] = max(geoms, key=lambda p: p.area)
        return self._derived["roi"]

    # ----------------------------------------------------------- mesh zones
    def mesh_zones(self):
        """Mesh-zone polygons with the derived columns the meshers need.

        Adds, per polygon:

        * ``_zone_type`` - ``channel`` / ``floodplain`` / ``refinement`` / ``other``,
          from the ``Zone Name`` field (:func:`classify_zone`);
        * ``_edge_length`` - target max edge length [m] from the
          ``Max Edge Length (m)`` field (``mesh.zone_size_field``), falling back to
          the configured per-type default, then multiplied by ``mesh.size_scale``;
        * ``_prio`` - overlap priority, finest intent winning.

        **Recomputed on every call, never cached**: ``mesh.size_scale`` is varied by
        the convergence study between builds of the same case, and a cached
        ``_edge_length`` would give every level the previous level's sizes.
        """
        cfg = self.cfg
        gdf = self.layer("mesh_zones", cfg.geodata.mesh_zones)
        name_field = match_field(gdf, cfg.mesh.zone_name_field)
        if name_field is None:
            raise ValueError(
                f"mesh_zones {Path(cfg.geodata.mesh_zones).name!r} has no "
                f"'{cfg.mesh.zone_name_field}' column (has {list(gdf.columns)})"
            )
        size_field = match_field(gdf, cfg.mesh.zone_size_field)
        defaults = {"channel": cfg.mesh.channel_size,
                    "floodplain": cfg.mesh.floodplain_size,
                    "refinement": cfg.mesh.refinement_size,
                    "other": cfg.mesh.floodplain_size}

        ztypes = [classify_zone(v) for v in gdf[name_field]]
        scale = float(cfg.mesh.size_scale or 1.0)
        edge = []
        raws = gdf[size_field] if size_field else [None] * len(gdf)
        for ztype, raw in zip(ztypes, raws):
            value = parse_decimal(raw)
            edge.append((value if (value is not None and value > 0.0)
                         else defaults[ztype]) * scale)
        return gdf.assign(_zone_type=ztypes, _edge_length=edge,
                          _prio=[ZONE_PRIORITY[zt] for zt in ztypes])

    def channel_union(self):
        """Channel footprint: union of the ``*channel*`` mesh zones, holes filled.

        Cacheable despite deriving from :meth:`mesh_zones`, because it uses only
        ``_zone_type`` and the geometry - neither of which ``mesh.size_scale``
        touches.
        """
        if "channel_union" not in self._derived:
            from shapely.ops import unary_union

            zones = self.mesh_zones()
            channel = zones[zones["_zone_type"] == "channel"]
            if channel.empty:
                raise ValueError(
                    "no mesh zone named '*channel*' in "
                    f"{Path(self.cfg.geodata.mesh_zones).name!r}; "
                    f"found {sorted(zones['_zone_type'].unique())}"
                )
            self._derived["channel_union"] = fill_holes(
                unary_union(channel.geometry.values))
        return self._derived["channel_union"]

    # ------------------------------------------------------------ centerline
    def centerline(self):
        """The channel centerline as a single merged LineString (longest part)."""
        if "centerline" not in self._derived:
            from shapely.ops import linemerge, unary_union

            gdf = self.layer("centerline", self.cfg.geodata.channel_centerline)
            merged = unary_union(gdf.geometry.values)
            line = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
            if line.geom_type == "MultiLineString":   # disjoint parts: take the longest
                line = max(line.geoms, key=lambda g: g.length)
            self._derived["centerline"] = line
        return self._derived["centerline"]

    def centerline_tangents(self, spacing: float):
        """Sample the centerline -> ``(points, unit tangents, KD-tree)``.

        Cached per *spacing*: the anisotropic mesher and the pre-wetting ask for
        different samplings of the same line, and each is a few thousand
        ``interpolate`` calls.
        """
        key = f"tangents:{float(spacing):.6g}"
        if key not in self._derived:
            from scipy.spatial import cKDTree

            line = self.centerline()
            count = max(2, int(line.length / spacing))
            distances = np.linspace(0.0, line.length, count)
            pts = np.array([[line.interpolate(d).x, line.interpolate(d).y]
                            for d in distances])
            tangents = np.gradient(pts, axis=0)
            tangents /= np.linalg.norm(tangents, axis=1, keepdims=True).clip(1e-9)
            self._derived[key] = (pts, tangents, cKDTree(pts))
        return self._derived[key]

    # ------------------------------------------------------------- roughness
    def roughness_zones(self):
        return self.layer("roughness_zones", self.cfg.geodata.roughness_zones)

    def roughness_table(self) -> dict[int, float]:
        """``{zone_id: roughness}`` from the zone-roughness CSV.

        The first column is the integer zone id, the second the roughness value (e.g.
        a Nikuradse k_s) that HydroBayesCal later adjusts. A header row is detected
        and skipped; further columns are ignored.
        """
        if "roughness_table" not in self._derived:
            self._derived["roughness_table"] = read_roughness_table(
                self.cfg.geodata.roughness_table)
        return self._derived["roughness_table"]

    # -------------------------------------------------------- other layers
    def lines(self, role: str, path):
        """Coordinate lists of every line in a layer (breaklines, and similar)."""
        gdf = self.layer(role, path)
        coords = []
        for geom in gdf.geometry.values:
            if geom.geom_type == "LineString":
                coords.append(list(geom.coords))
            elif geom.geom_type == "MultiLineString":
                coords.extend(list(part.coords) for part in geom.geoms)
        return coords

    def region_seeds(self):
        """``(xy, matid)`` of the region seed points, or ``(None, None)``."""
        if self.cfg.geodata.region_points is None:
            return None, None
        gdf = self.layer("region_points", self.cfg.geodata.region_points)
        matid_col = next(
            (c for c in gdf.columns if c.upper() in ("MATID", "FRIC_ID", "MAT_ID")),
            None)
        xy = np.array([[g.x, g.y] for g in gdf.geometry.values])
        matids = (gdf[matid_col].astype(int).to_numpy() if matid_col
                  else np.ones(len(gdf), dtype=int))
        return xy, matids

    def liquid_boundaries(self):
        """The raw inflow/outflow (and internal) boundary-line layer.

        One of the most re-read layers in the codebase: the steering writer, the
        boundary classifier, the water-table fit and both meshers all want it, and
        each used to open it again.
        """
        return self.layer("liquid_boundaries", self.cfg.boundaries.liquid_boundaries)

    def structures(self):
        """The raw dam / weir / wall / building layer."""
        return self.layer("structures", self.cfg.geodata.structures)

    def centerline_frame(self):
        """The raw centerline layer (callers that want the geometries, not tangents)."""
        return self.layer("centerline", self.cfg.geodata.channel_centerline)


def read_roughness_table(path) -> dict[int, float]:
    """Standalone reader for the zone-roughness CSV (no Dataset required)."""
    import pandas as pd

    df = pd.read_csv(Path(path), header=None)
    first = df.iloc[0]
    if pd.to_numeric(first.iloc[:2], errors="coerce").isna().any():
        df = df.iloc[1:]                         # the first row was a header
    ids = pd.to_numeric(df.iloc[:, 0], errors="coerce")
    values = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    return {int(i): float(v) for i, v in zip(ids, values)
            if not (np.isnan(i) or np.isnan(v))}


def nominal_channel_size(cfg) -> float:
    """Effective (scaled) target channel edge length [m].

    The smallest channel-zone ``_edge_length`` from the mesh-zones gpkg when
    configured (per-zone ``Max Edge Length (m)`` values override the ``*_size``
    fallbacks, so ``channel_size`` alone can misstate the built resolution), else
    ``channel_size * size_scale``. Both already include ``mesh.size_scale``.
    """
    fallback = float(cfg.mesh.channel_size * cfg.mesh.size_scale)
    if cfg.geodata.mesh_zones is None:
        return fallback
    try:
        zones = dataset(cfg).mesh_zones()
        channel = zones[zones["_zone_type"] == "channel"]
        if channel.empty:
            return fallback
        return float(channel["_edge_length"].min())
    except Exception as exc:  # pragma: no cover - geodata unavailable
        log.debug("nominal_channel_size fell back to config sizes: %s", exc)
        return fallback


def dataset(cfg) -> Dataset:
    """The :class:`Dataset` for *cfg*, created once and kept on the config.

    Attached to the ``Config`` instance rather than held in a module-level cache so
    its lifetime is the config's: two configs in one process (the convergence study
    holds several) never share a cache, and nothing has to be invalidated by hand.
    """
    existing = cfg.__dict__.get("_geodata")
    if existing is None or existing.cfg is not cfg:
        existing = Dataset(cfg)
        cfg.__dict__["_geodata"] = existing
    return existing
