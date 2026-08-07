"""Where a gain-lose reach exchanges water, derived from the water table.

A river that loses flow into a porous body and regains it downstream has no sharp
exchange faces: it is genuinely fuzzy where infiltration begins and ends. Asking the
user to draw the two faces therefore asks for a number they do not have.

The water table supplies it instead. The body's saturated zone is bounded by the two
channel levels it exchanges with, so once the phreatic surface is known
(:mod:`hydromate.watertable`) each node's role follows from a head comparison:

* it **loses** where it is wet and its free surface stands **above** the table -
  there is head pushing water down into the body;
* it **gains** where the table stands **above the bed** - the saturated zone cuts the
  ground surface and groundwater emerges.

Both conditions are evaluated inside the zone polygon, optionally buffered
(``gain_lose.zone_buffer``): a bar polygon usually outlines the bar itself, while the
water it takes in is in the channel *beside* it, so the losing face has to be allowed
to reach the adjacent wetted edge.

This module is the build-time view - it reports the faces and the exchange they imply
so both are visible **before** a run. The generated ``USER_RAIN`` routine
(:mod:`hydromate.fortran`) applies the same two rules at run time, so the faces move
with the stage instead of being frozen at build time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("hydromate")

#: head [m] a node needs before it counts as losing or gaining (DEM/solver noise)
HEAD_TOLERANCE = 0.01


@dataclass
class ExchangeFaces:
    """The losing and gaining faces of a gain-lose reach at one water level."""

    losing: object          # boolean (NPOIN,) node mask
    gaining: object         # boolean (NPOIN,) node mask
    losing_area: float      # [m2]
    gaining_area: float     # [m2]
    losing_head: float      # mean (surface - table) over the losing face [m]
    gaining_head: float     # mean (table - bed) over the gaining face [m]
    plane: object           # the PhreaticPlane they were derived from

    def summary(self) -> list[str]:
        return [
            (f"gain-lose faces: losing {self.losing_area:,.0f} m2 "
             f"(mean head {self.losing_head:.3f} m), gaining "
             f"{self.gaining_area:,.0f} m2 (mean head {self.gaining_head:.3f} m)"),
        ]


def zone_mask(cfg, mesh, *, buffer: float | None = None):
    """Nodes inside the porous body, buffered by ``gain_lose.zone_buffer``.

    The buffer defaults to one channel cell: without it the losing face of a bar
    polygon is only the bar's own toe, while the water actually being lost is in the
    channel beside it.
    """
    import numpy as np
    import shapely
    from shapely.ops import unary_union

    from hydromate.boundary import _percolation_patches

    patches = _percolation_patches(cfg)
    if not patches:
        return np.zeros(len(mesh.x), dtype=bool)
    if buffer is None:
        buffer = cfg.gain_lose.zone_buffer
    if buffer is None:
        buffer = float(cfg.mesh.channel_size) * float(cfg.mesh.size_scale)
    poly = unary_union([p["geom"] for p in patches])
    if buffer:
        poly = poly.buffer(float(buffer))
    return np.asarray(shapely.contains(poly, shapely.points(mesh.x, mesh.y)))


def derive_faces(cfg, mesh, plane, surface, *, tol: float = HEAD_TOLERANCE
                 ) -> ExchangeFaces:
    """Split the porous body into its losing and gaining faces at *surface*.

    *surface* is a per-node free-surface elevation (bed + depth). A node is assigned
    to at most one face: standing water pushing head *into* the body wins over
    emergence, so a node whose surface is above the table loses even if the table is
    also above its bed.

    Raises when either face is empty - a gain-lose reach with nowhere to lose or
    nowhere to gain is a geometry error the user needs to see, not something to paper
    over with a zero exchange.
    """
    import numpy as np

    from hydromate.watertable import water_table_depth

    if plane is None:
        raise ValueError(
            "gain-lose faces need a water table; none could be fitted (see the "
            "warnings above - set gain_lose.water_table_levels, or use faces: lines)"
        )
    inside = zone_mask(cfg, mesh)
    bottom = np.asarray(mesh.bottom, dtype=float)
    surface = np.asarray(surface, dtype=float)
    table = plane.elevation(np.asarray(mesh.x, float), np.asarray(mesh.y, float))

    wet = surface > bottom + tol
    losing = inside & wet & (surface > table + tol)
    gaining = inside & (table > bottom + tol) & ~losing

    from hydromate.wetting import _nodal_areas

    area = _nodal_areas(mesh.x, mesh.y, np.asarray(mesh.triangles))
    if not losing.any() or not gaining.any():
        held = water_table_depth(plane, mesh, inside)
        raise ValueError(
            f"gain-lose faces are incomplete: {int(losing.sum())} losing and "
            f"{int(gaining.sum())} gaining node(s) inside the zone "
            f"({area[inside].sum():.0f} m2, {float(held.max()):.2f} m max table "
            "depth). Either the zone does not reach the wetted channel (raise "
            "gain_lose.zone_buffer) or the table does not cut the ground anywhere "
            "(check gain_lose.water_table_levels)."
        )

    faces = ExchangeFaces(
        losing=losing, gaining=gaining,
        losing_area=float(area[losing].sum()),
        gaining_area=float(area[gaining].sum()),
        losing_head=float(np.mean((surface - table)[losing])),
        gaining_head=float(np.mean((table - bottom)[gaining])),
        plane=plane,
    )
    for line in faces.summary():
        log.info("  %s", line)
    return faces


def estimate_exchange(cfg, mesh, faces: ExchangeFaces, surface) -> float:
    """Discharge [m3/s] the losing face implies at the configured ``conductivity``.

    Green-Ampt's saturated limit per node, ``f = kf (h + Lz + hf) / Lz`` with *h* the
    head over the table, integrated over the losing face. Logged at build time so the
    modelled exchange is visible before a run - it is easy to be an order of
    magnitude out on kf, and this is the number to compare against a measured one.
    """
    import numpy as np

    from hydromate.wetting import _nodal_areas

    kf = cfg.gain_lose.conductivity
    if kf is None:
        return float("nan")
    lz = float(cfg.gain_lose.porous_depth)
    hf = float(cfg.gain_lose.suction)
    surface = np.asarray(surface, dtype=float)
    table = faces.plane.elevation(np.asarray(mesh.x, float), np.asarray(mesh.y, float))
    head = np.maximum(surface - table, 0.0)[faces.losing]
    area = _nodal_areas(mesh.x, mesh.y, np.asarray(mesh.triangles))[faces.losing]
    rate = np.minimum(float(kf) * (head + lz + hf) / lz,
                      float(cfg.gain_lose.max_rate))
    q = float((rate * area).sum())
    target = cfg.gain_lose.discharge
    if target is not None:
        log.info("  gain-lose exchange: %.4f m3/s prescribed (kf would give %.4f)",
                 float(target), q)
    else:
        log.info("  gain-lose exchange: %.4f m3/s at kf=%.3g m/s over %.0f m2",
                 q, float(kf), faces.losing_area)
    return q


__all__ = ["ExchangeFaces", "derive_faces", "estimate_exchange", "zone_mask"]
