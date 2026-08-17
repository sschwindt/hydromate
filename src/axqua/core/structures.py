"""Dams, weirs, walls and buildings - authored in QGIS, meshed by either solver.

Why there is no STL here
-----------------------
The usual reason a river CFD workflow needs an STL is ``snappyHexMesh``: it meshes a
background block and *snaps* it onto a triangulated surface, so every solid object has
to arrive as triangles. axqua does not use snappyHexMesh (see
:mod:`axqua.solvers.openfoam.polymesh` for why), and writes ``constant/polyMesh``
directly. That removes the requirement entirely: a structure only has to tell the
mesher **where its footprint is** and **how high it stands**, and both are ordinary
vector attributes.

So a structure layer is a plain GeoPackage or shapefile that QGIS authors natively -
digitise the footprint, type in a crest level - with no CAD step, no triangulation,
and no format QGIS cannot round-trip.

Geometry
--------
* **Polygons** are the footprint directly (a building, a dam body, a pier).
* **Lines** are buffered by their width attribute to give a footprint. This is the
  natural way to draw a wall or a dam crest: you trace the crest and say how thick it
  is, rather than digitising two parallel sides by hand.

Two modes, and the choice is hydraulic
--------------------------------------
``overflow`` (dam, weir, embankment, levee)
    The bed is **raised to the crest**. Water passes over the structure when the level
    exceeds it, and the mesh is unchanged in plan - the structure is simply terrain
    that was not in the DEM. This is what a dam or weir *is* to a depth-averaged or
    free-surface model, and it works identically in both solvers.

``solid`` (wall, floodwall, building, pier)
    The footprint is **removed from the domain**, so its sides become no-slip walls
    running from bed to lid. Use it for anything that is never overtopped: it is both
    cheaper (no cells inside the obstacle) and more correct (a genuine vertical face,
    rather than a very steep bed with a sliver of cells on top).

Height: two ways, meaning two different things
----------------------------------------------
* a **crest elevation** (m a.s.l.) gives a *level* crest - a dam, a weir, a floodwall;
* a **height** (m above the local ground) gives a crest that *follows the terrain* -
  an embankment or levee of constant build height.

Both are common; neither is a good default for the other, so the field you fill in is
what decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("axqua")

SOLID = "solid"
OVERFLOW = "overflow"

# Substrings that classify a structure when no explicit mode column is present.
# "block" is deliberately absent: a *block ramp* is an overflow structure while a
# *concrete block* is solid, so the word carries no usable signal on its own.
_SOLID_WORDS = ("wall", "building", "pier", "abutment", "sheet pile", "sheetpile")
_OVERFLOW_WORDS = ("dam", "weir", "embankment", "levee", "dyke", "dike", "sill",
                   "ramp", "spillway", "groyne", "groin")


def classify_structure(text: str) -> str:
    """Map a structure's type text to ``solid`` or ``overflow``.

    Same substring convention as the mesh zones (:func:`axqua.core.geodata.
    classify_zone`), so a user who has learned one has learned the other. Anything
    unrecognised is ``overflow``: raising the bed is the conservative failure, since
    it keeps the domain connected and lets water pass, whereas wrongly blanking a
    footprint would silently wall off part of the reach.
    """
    lowered = str(text).lower()
    if any(word in lowered for word in _SOLID_WORDS):
        return SOLID
    if any(word in lowered for word in _OVERFLOW_WORDS):
        return OVERFLOW
    return OVERFLOW


@dataclass
class Structure:
    """One dam, weir, wall or building, ready to apply to a mesh."""

    name: str
    mode: str                      # SOLID | OVERFLOW
    polygon: object                # shapely Polygon/MultiPolygon footprint
    crest: float | None = None     # level crest [m a.s.l.]
    height: float | None = None    # or: constant height above local ground [m]

    @property
    def describes_level_crest(self) -> bool:
        return self.crest is not None

    def summary(self) -> str:
        how = (f"crest {self.crest:.3f} m a.s.l." if self.describes_level_crest
               else f"{self.height:g} m above ground")
        return (f"{self.name}: {self.mode}, {how}, "
                f"{self.polygon.area:,.0f} m2 footprint")


def _first_field(gdf, *candidates: str) -> str | None:
    """Case-insensitive lookup of the first present column among *candidates*."""
    lowered = {str(c).lower(): c for c in gdf.columns}
    for candidate in candidates:
        if candidate and candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def load_structures(cfg) -> list[Structure]:
    """Read ``geodata.structures`` into :class:`Structure` objects.

    Lines are buffered to their width (``width_field``, else
    ``structures.default_width``) with flat ends, so a wall drawn as a single line
    becomes a rectangle of the right thickness rather than a lozenge with rounded
    caps that would over-block the channel at each end.
    """
    from axqua.core.geodata import dataset
    from axqua.core.geodata import parse_decimal

    sc = cfg.structures
    if not sc.enabled or cfg.geodata.structures is None:
        return []

    gdf = dataset(cfg).structures()
    type_field = _first_field(gdf, sc.type_field, "type", "structure", "class")
    mode_field = _first_field(gdf, sc.mode_field, "mode")
    crest_field = _first_field(gdf, sc.crest_field, "crest", "crest_m", "z")
    height_field = _first_field(gdf, sc.height_field, "height", "height_m")
    width_field = _first_field(gdf, sc.width_field, "width", "thickness")
    name_field = _first_field(gdf, sc.name_field, "name", "id")

    if crest_field is None and height_field is None:
        raise ValueError(
            f"structures layer {Path(cfg.geodata.structures).name!r} has neither a "
            f"crest field ({sc.crest_field!r}) nor a height field "
            f"({sc.height_field!r}); one is needed to know how high the structure "
            "stands. Add a crest elevation (m a.s.l.) for a level crest, or a height "
            "(m above ground) for an embankment that follows the terrain.")

    out: list[Structure] = []
    for index, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type in ("LineString", "MultiLineString"):
            width = (parse_decimal(row[width_field]) if width_field else None)
            width = width if (width and width > 0) else sc.default_width
            geom = geom.buffer(width / 2.0, cap_style=2, join_style=2)  # flat, mitred
        elif geom.geom_type not in ("Polygon", "MultiPolygon"):
            log.warning("structure %s: unsupported geometry %s; skipped",
                        index, geom.geom_type)
            continue

        mode = None
        if mode_field:
            mode = str(row[mode_field]).strip().lower() or None
            if mode not in (SOLID, OVERFLOW):
                mode = None
        if mode is None:
            mode = classify_structure(row[type_field]) if type_field else OVERFLOW

        crest = parse_decimal(row[crest_field]) if crest_field else None
        height = parse_decimal(row[height_field]) if height_field else None
        if crest is None and height is None:
            log.warning("structure %s has neither crest nor height; skipped", index)
            continue

        name = str(row[name_field]) if name_field else f"structure-{index}"
        out.append(Structure(name=name, mode=mode, polygon=geom, crest=crest,
                             height=height))

    for structure in out:
        log.info("  %s", structure.summary())
    return out


def _inside(polygon, xy: np.ndarray) -> np.ndarray:
    """Boolean mask of the points inside *polygon* (vectorised)."""
    import shapely

    if xy.size == 0:
        return np.zeros(0, dtype=bool)
    return shapely.contains_xy(polygon, xy[:, 0], xy[:, 1])


def solid_mask(structures: list[Structure], xy: np.ndarray) -> np.ndarray:
    """Points that fall inside a ``solid`` structure and must leave the domain."""
    mask = np.zeros(len(xy), dtype=bool)
    for structure in structures:
        if structure.mode == SOLID:
            mask |= _inside(structure.polygon, xy)
    return mask


def apply_to_bed(structures: list[Structure], xy: np.ndarray,
                 bed: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Raise *bed* to every ``overflow`` structure's crest.

    Returns ``(bed, touched)``. The bed is only ever raised, never lowered
    (``maximum``): a structure adds material to the terrain, and a crest digitised
    slightly below the surveyed ground should not carve a trench through it.
    """
    bed = np.array(bed, dtype=float, copy=True)
    touched = np.zeros(len(bed), dtype=bool)
    for structure in structures:
        if structure.mode != OVERFLOW:
            continue
        inside = _inside(structure.polygon, xy)
        if not inside.any():
            log.warning("structure %s covers no mesh point - is it inside the ROI, "
                        "and is the layer in the project CRS?", structure.name)
            continue
        if structure.describes_level_crest:
            crest = float(structure.crest)
        else:
            # a height is "above the local ground", so the crest follows the terrain
            crest = bed[inside] + float(structure.height)
        raised = np.maximum(bed[inside], crest)
        gain = float(np.max(raised - bed[inside])) if inside.any() else 0.0
        bed[inside] = raised
        touched |= inside
        log.info("  %s: raised %d points, up to +%.3f m", structure.name,
                 int(inside.sum()), gain)
    return bed, touched


def solid_footprint(structures: list[Structure]):
    """Union of the ``solid`` footprints, or ``None`` when there are none."""
    solids = [s.polygon for s in structures if s.mode == SOLID]
    if not solids:
        return None
    from shapely.ops import unary_union

    return unary_union(solids)


def report(structures: list[Structure]) -> list[str]:
    """Printable summary for a build report."""
    if not structures:
        return []
    solid = sum(1 for s in structures if s.mode == SOLID)
    overflow = len(structures) - solid
    out = [f"structures: {len(structures)} ({overflow} overflow, {solid} solid)"]
    out += [f"    {s.summary()}" for s in structures]
    return out
