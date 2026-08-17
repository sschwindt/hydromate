"""The water table under a porous gravel bar, as a phreatic plane.

A losing-gaining gravel bar exchanges with the channel at two places: it takes water
in at the **losing** line and returns it at the **gaining** line (see
:mod:`axqua.solvers.telemac.fortran`). The bar's saturated zone is therefore bounded by two known
water levels, and the surface joining them *is* the water table of that through-flow -
its gradient is what drives the exchange in the first place. Over a bar of a hundred
metres or so that surface is planar to within a few centimetres, so it is represented
here as

.. math:: z_{wt}(x, y) = c_0 + c_x (x - x_0) + c_y (y - y_0)

fitted by least squares through sample points along the internal lines, each carrying
its own water level.

Two consumers need it, and they need it to agree:

* the **pre-wet** (:func:`axqua.steering.write_initial_conditions`) seeds any bar
  ground lying below the table, because a closed depression on a bar *above* the
  channel can never fill from the surface - no flow path reaches it - and the
  drainable-seed filter deliberately refuses to seed it. Without this a real pool
  stays dry for the whole run;
* the **patch drain** in the generated ``USER_RAIN`` routine tapers to zero at the
  table rather than at an absolute depth, so it clears standing water off the bar top
  but cannot empty a pool that cuts below the water table.

Because a plane is five numbers, the Fortran side needs no per-node array: the
coefficients are baked into the generated routine.

**The plane is only meaningful inside the patch.** Extended across a whole reach it
sits above any ground lower than the bar - on isar-2025, 22 598 m² of it. Every use
therefore goes through :func:`water_table_depth`, which is the single place the
clip-to-the-patch rule lives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from axqua.core.geodata import dataset

log = logging.getLogger("axqua")

#: sample spacing along an internal line when fitting the plane [m]
SAMPLE_SPACING = 2.0
#: warn when the plane cannot represent the supplied levels this closely [m]
MAX_RESIDUAL = 0.15


@dataclass
class PhreaticPlane:
    """A planar water table ``z = c0 + cx (x - x0) + cy (y - y0)`` [m a.s.l.]."""

    c0: float
    cx: float
    cy: float
    x0: float
    y0: float
    levels: dict          # the source water level of each internal line
    residual: float = 0.0  # max |fit - level| over the sample points [m]

    @property
    def gradient(self) -> float:
        """Slope of the table [-] (its steepest descent)."""
        return float((self.cx ** 2 + self.cy ** 2) ** 0.5)

    def elevation(self, x, y):
        """Water-table elevation at *x*, *y* (arrays or scalars)."""
        return self.c0 + self.cx * (x - self.x0) + self.cy * (y - self.y0)


def _internal_lines(cfg):
    """``[(tag, geometry), ...]`` for the ``int-*`` lines of the liquid boundaries."""
    from axqua.solvers.telemac.boundary import _is_internal, _type_column

    gdf = dataset(cfg).liquid_boundaries()
    col = _type_column(gdf)
    if col is None:
        return []
    return [(str(row[col]), row.geometry) for _, row in gdf.iterrows()
            if _is_internal(row[col])]


def _level_from_surface(geometry, mesh, surface, *, radius: float = 6.0):
    """Median water level of *surface* on the wetted nodes within *radius* of a line.

    *surface* is a per-node water-surface elevation (bed + seeded depth at build time);
    nodes where it does not stand above the bed are ignored, so a dry strip returns
    ``None`` rather than a bed elevation.
    """
    import numpy as np
    import shapely

    dist = np.asarray(shapely.distance(shapely.points(mesh.x, mesh.y), geometry))
    bottom = np.asarray(mesh.bottom, dtype=float)
    near = (dist < radius) & (np.asarray(surface, dtype=float) > bottom + 0.01)
    if near.sum() < 5:
        return None
    return float(np.median(np.asarray(surface, dtype=float)[near]))


def _samples_from_lines(cfg, mesh, surface, overrides):
    """Fit samples taken along the ``int-*`` lines (``faces: lines``)."""
    import numpy as np

    from axqua.solvers.telemac.boundary import _internal_sign

    lines = _internal_lines(cfg)
    if len(lines) < 2:
        log.warning("  water table: need two internal lines to fit a plane, found %d",
                    len(lines))
        return [], [], [], {}

    xs, ys, zs, levels = [], [], [], {}
    for tag, geom in lines:
        key = "losing" if _internal_sign(tag) < 0 else "gaining"
        level = overrides.get(key)
        source = "config"
        if level is None and surface is not None:
            level = _level_from_surface(geom, mesh, surface)
            source = "seeded surface"
        if level is None:
            log.warning("  water table: no level for internal line %r "
                        "(dry at build time and no water_table_levels override)", tag)
            continue
        levels[key] = float(level)
        n = max(2, int(geom.length / SAMPLE_SPACING))
        for t in np.linspace(0.0, geom.length, n):
            p = geom.interpolate(t)
            xs.append(p.x)
            ys.append(p.y)
            zs.append(float(level))
        log.info("  water table: %s line %r at %.3f m (%s)", key, tag, level, source)
    return xs, ys, zs, levels


def _samples_from_zone(cfg, mesh, surface, overrides, *, band: float = 10.0):
    """Fit samples taken at the two ends of the **zone's** reach extent.

    This is what makes the drawn lines optional. The zone is projected onto the
    channel centerline, and the channel water surface is measured in a band at each
    end of its station range: those two levels bound the bar's saturated zone exactly
    as the drawn lines did, without asking the user to decide where percolation
    begins - which is the fuzzy part.

    Which end loses and which gains follows from the head: water enters the porous
    body where the surface stands higher. Nothing assumes a digitising direction.
    """
    import numpy as np

    from axqua.solvers.telemac.steering import _centerline_arclength

    zone = patch_node_mask(cfg, mesh)
    if not zone.any():
        log.warning("  water table: gain_lose.zone contains no mesh node")
        return [], [], [], {}
    if cfg.geodata.channel_centerline is None:
        log.warning("  water table: faces from the zone need geodata.channel_centerline")
        return [], [], [], {}

    station = _centerline_arclength(cfg, mesh)
    bottom = np.asarray(mesh.bottom, dtype=float)
    ends = (float(station[zone].min()), float(station[zone].max()))

    found = []
    for end in ends:
        near = np.abs(station - end) < band
        if surface is not None:
            near &= np.asarray(surface, dtype=float) > bottom + 0.01
        if near.sum() < 5:
            continue
        level = (float(np.median(np.asarray(surface, dtype=float)[near]))
                 if surface is not None else None)
        found.append((end, level, near))

    if len(found) < 2:
        log.warning("  water table: the zone's two reach ends are not both wetted at "
                    "build time (%d of 2) - draw the int-* lines (faces: lines) or "
                    "set water_table_levels", len(found))
        return [], [], [], {}

    # the higher water surface is where the body takes water in
    found.sort(key=lambda f: (f[1] if f[1] is not None else 0.0), reverse=True)
    keys = ("losing", "gaining")
    xs, ys, zs, levels = [], [], [], {}
    for key, (end, level, near) in zip(keys, found):
        if overrides.get(key) is not None:
            level, source = float(overrides[key]), "config"
        else:
            source = "channel surface at the zone's %s end" % (
                "upstream" if key == "losing" else "downstream")
        levels[key] = float(level)
        xs += list(mesh.x[near])
        ys += list(mesh.y[near])
        zs += [float(level)] * int(near.sum())
        log.info("  water table: %s end at station %.1f m -> %.3f m (%s)",
                 key, end, level, source)
    return xs, ys, zs, levels


def fit_phreatic_plane(cfg, mesh, *, surface=None, faces=None) -> PhreaticPlane | None:
    """Fit the water table of the porous body.

    Two ways to find the two bounding levels, chosen by *faces* (default
    ``gain_lose.faces``):

    * ``water-table`` - from the **zone polygon** alone: the channel water surface at
      each end of the zone's reach extent (:func:`_samples_from_zone`);
    * ``lines`` - from the ``int-*`` lines the user drew (:func:`_samples_from_lines`).

    ``gain_lose.water_table_levels`` overrides either. Returns ``None`` (with a
    warning) when fewer than two levels can be established, since a plane through one
    level is not determined.
    """
    import numpy as np

    overrides = cfg.gain_lose.water_table_levels or {}
    faces = faces or cfg.gain_lose.faces
    if faces == "lines":
        xs, ys, zs, levels = _samples_from_lines(cfg, mesh, surface, overrides)
    else:
        xs, ys, zs, levels = _samples_from_zone(cfg, mesh, surface, overrides)

    if len(levels) < 2:
        log.warning("  water table: only %d level(s) available - no plane fitted",
                    len(levels))
        return None

    x = np.asarray(xs)
    y = np.asarray(ys)
    z = np.asarray(zs)
    x0, y0 = float(x.mean()), float(y.mean())
    basis = np.column_stack([np.ones_like(x), x - x0, y - y0])
    coeff, *_ = np.linalg.lstsq(basis, z, rcond=None)
    residual = float(np.abs(z - basis @ coeff).max())
    plane = PhreaticPlane(c0=float(coeff[0]), cx=float(coeff[1]), cy=float(coeff[2]),
                          x0=x0, y0=y0, levels=levels, residual=residual)
    log.info("  water table: phreatic plane fitted, gradient %.2f permille, "
             "residual %.3f m", 1000.0 * plane.gradient, residual)
    if residual > MAX_RESIDUAL:
        log.warning("  water table: the two line levels are not co-planar to within "
                    "%.2f m (residual %.3f m) - check that both lines are wetted and "
                    "roughly perpendicular to the bar axis", MAX_RESIDUAL, residual)
    return plane


def patch_node_mask(cfg, mesh):
    """Boolean (NPOIN,) flag of the nodes inside the percolation patch(es)."""
    import numpy as np
    import shapely
    from shapely.ops import unary_union

    from axqua.solvers.telemac.boundary import _percolation_patches

    patches = _percolation_patches(cfg)
    if not patches:
        return np.zeros(len(mesh.x), dtype=bool)
    poly = unary_union([p["geom"] for p in patches])
    return np.asarray(shapely.contains(poly, shapely.points(mesh.x, mesh.y)))


def water_table_depth(plane: PhreaticPlane, mesh, mask):
    """Water depth [m] the table supports: ``max(table - bed, 0)`` inside *mask*.

    Zero everywhere outside *mask*. This is the **only** place the clip is applied:
    a plane fitted to a gravel bar is meaningless across the rest of a reach, where it
    would stand above every bed lower than the bar.
    """
    import numpy as np

    if plane is None:
        return np.zeros(len(mesh.x), dtype=float)
    table = plane.elevation(np.asarray(mesh.x, dtype=float),
                            np.asarray(mesh.y, dtype=float))
    depth = np.maximum(table - np.asarray(mesh.bottom, dtype=float), 0.0)
    return np.where(np.asarray(mask, dtype=bool), depth, 0.0)


def describe(plane: PhreaticPlane | None, mesh, mask, area=None) -> str:
    """One-line summary of what a table would wet (for the build log)."""
    import numpy as np

    if plane is None:
        return "water table: not fitted"
    depth = water_table_depth(plane, mesh, mask)
    wet = depth > 0
    if area is None:
        return (f"water table wets {int(wet.sum())} nodes, max depth "
                f"{float(depth.max()):.2f} m")
    return (f"water table wets {float(area[wet].sum()):.0f} m2 / "
            f"{float((depth * area).sum()):.1f} m3 inside the patch "
            f"(max depth {float(depth.max()):.2f} m); "
            f"{float(area[np.asarray(mask, bool) & ~wet].sum()):.0f} m2 of the patch "
            "stays dry")


__all__ = [
    "PhreaticPlane",
    "describe",
    "fit_phreatic_plane",
    "patch_node_mask",
    "water_table_depth",
]
