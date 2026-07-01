"""Generate an outflow stage-discharge rating curve from normal-flow hydraulics.

For a steady simulation the downstream (outflow) boundary needs the water-surface
elevation at the simulated discharge. When no *measured* rating curve is
available, this module synthesises one from a roughness coefficient and a
prismatic channel cross-section by inverting Manning's uniform- (normal-) flow
equation::

    Q = (1 / n) * A(h) * R(h) ** (2/3) * sqrt(S0)

for a trapezoidal section with bottom width ``b`` and side slope ``m``
(horizontal:vertical, ``m = 0`` is rectangular)::

    A(h) = (b + m * h) * h                      # flow area
    P(h) = b + 2 * h * sqrt(1 + m ** 2)         # wetted perimeter
    R(h) = A(h) / P(h)                           # hydraulic radius

``Q`` is strictly increasing in depth, so the normal depth ``h_n`` conveying a
given ``Q`` is found by bisection; the stage is ``WSE = bed_elevation + h_n``.

Roughness is given either as a Manning ``n`` or a Strickler ``Kst`` (``n = 1/Kst``).
The result is written as a ``Q,WSE,depth`` CSV that :func:`hydromate.hydraulics.
read_stage_discharge` consumes as ``boundaries.stage_discharge``.
"""

from __future__ import annotations

import csv
import logging
import math
from pathlib import Path
from typing import Iterable

log = logging.getLogger("hydromate")


def _resolve_n(manning: float | None, strickler: float | None) -> float:
    """Return Manning's n from exactly one of (manning, strickler=Kst=1/n)."""
    if (manning is None) == (strickler is None):
        raise ValueError("provide exactly one of manning (n) or strickler (Kst)")
    n = manning if manning is not None else 1.0 / float(strickler)
    if n <= 0:
        raise ValueError(f"roughness must be positive, got Manning n={n}")
    return n


def _conveyance_q(h: float, n: float, slope: float,
                  bottom_width: float, side_slope: float) -> float:
    """Manning discharge for depth *h* in a trapezoidal section (0 if h<=0)."""
    if h <= 0.0:
        return 0.0
    area = (bottom_width + side_slope * h) * h
    perim = bottom_width + 2.0 * h * math.sqrt(1.0 + side_slope ** 2)
    radius = area / perim
    return (1.0 / n) * area * radius ** (2.0 / 3.0) * math.sqrt(slope)


def normal_depth(discharge: float, *, manning: float | None = None,
                 strickler: float | None = None, slope: float,
                 bottom_width: float, side_slope: float = 0.0,
                 tol: float = 1e-6) -> float:
    """Normal (uniform-flow) depth [m] conveying *discharge* [m3/s].

    Roughness via *manning* (n) or *strickler* (Kst). *slope* is the longitudinal
    bed slope S0 (m/m, > 0); *bottom_width* and *side_slope* (H:V) define the
    trapezoidal section. Solved by bisection on the (monotonic) Manning equation.
    """
    n = _resolve_n(manning, strickler)
    if slope <= 0.0:
        raise ValueError(f"bed slope must be positive, got {slope}")
    if bottom_width < 0.0:
        raise ValueError(f"bottom_width must be >= 0, got {bottom_width}")
    if discharge <= 0.0:
        return 0.0

    def q(h: float) -> float:
        return _conveyance_q(h, n, slope, bottom_width, side_slope)

    hi = 1.0
    for _ in range(80):                       # grow until the section conveys Q
        if q(hi) >= discharge:
            break
        hi *= 2.0
    else:
        raise ValueError(f"could not bracket a normal depth for Q={discharge}")
    lo = 0.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if q(mid) < discharge:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def generate_stage_discharge(out: str | Path, discharges: Iterable[float] | float,
                             *, manning: float | None = None,
                             strickler: float | None = None, slope: float,
                             bottom_width: float, side_slope: float = 0.0,
                             bed_elevation: float = 0.0) -> Path:
    """Write a normal-flow ``Q,WSE,depth`` rating CSV for *discharges* to *out*.

    For a steady run a single discharge (the simulated Q) is enough; pass several
    to tabulate a curve. ``WSE = bed_elevation + normal_depth(Q)``.
    """
    qs = [float(discharges)] if isinstance(discharges, (int, float)) else \
        sorted(float(q) for q in discharges)
    if not qs:
        raise ValueError("no discharges given")

    out = Path(out)
    rows = []
    for q in qs:
        h = normal_depth(q, manning=manning, strickler=strickler, slope=slope,
                         bottom_width=bottom_width, side_slope=side_slope)
        rows.append((q, bed_elevation + h, h))

    with open(out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Q", "WSE", "depth"])
        for q, wse, h in rows:
            writer.writerow([f"{q:.4f}", f"{wse:.4f}", f"{h:.4f}"])
    n = manning if manning is not None else 1.0 / float(strickler)
    log.info("wrote normal-flow rating curve (%d point(s), Manning n=%.4f) -> %s",
             len(rows), n, out)
    return out


def synthesize_outflow_rating(cfg, discharge, *, side_slope: float = 0.0,
                              slope: float | None = None,
                              bed_elevation: float | None = None,
                              width: float | None = None,
                              manning: float | None = None,
                              strickler: float | None = None,
                              out=None) -> Path:
    """Derive an outflow stage-discharge curve from the case geodata + config.

    Unless given explicitly, the trapezoidal-channel parameters are taken from the
    geodata: *width* = total length of the outflow liquid-boundary line(s)
    (``boundaries.liquid_boundaries``, field tagged ``outflow``); *bed_elevation* =
    thalweg (minimum DEM elevation) sampled along that line; *slope* = reach bed
    slope from a linear fit of the DEM along ``geodata.channel_centerline``. The
    roughness defaults to the lateral-boundary friction in the config
    (``friction.boundary_law``/``boundary_coefficient``; Strickler or Manning).
    Writes a one-row ``Q,WSE,depth`` CSV to *out* (default ``boundaries.stage_discharge``).
    """
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.warp import transform as warp_transform
    from shapely.ops import unary_union

    from hydromate.boundary import _normalise_kind, _type_column

    if manning is None and strickler is None:        # roughness from the config
        law, coef = cfg.friction.boundary_law, cfg.friction.boundary_coefficient
        if law == 3:
            strickler = coef
        elif law == 4:
            manning = coef
        else:
            raise ValueError("pass manning/strickler: friction.boundary_law is "
                             f"{law} (not Strickler=3 or Manning=4)")

    # outflow boundary line(s) -> width + sampling geometry
    lb = gpd.read_file(cfg.boundaries.liquid_boundaries)
    if lb.crs and lb.crs.to_epsg() != cfg.crs_epsg:
        lb = lb.to_crs(epsg=cfg.crs_epsg)
    type_col = _type_column(lb)
    if type_col is None:
        raise ValueError(f"{Path(cfg.boundaries.liquid_boundaries).name}: no inflow/"
                         "outflow type column to find the outflow line")
    outflow = lb[[_normalise_kind(v) == "outflow" for v in lb[type_col]]]
    if outflow.empty:
        raise ValueError(f"no 'outflow' line in {Path(cfg.boundaries.liquid_boundaries).name}")
    outline = unary_union(outflow.geometry.values)
    if width is None:
        width = float(outflow.length.sum())

    with rasterio.open(cfg.geodata.dem_initial) as dem:
        def _sample(geom, n):
            d = np.linspace(0.0, geom.length, n)
            pts = [geom.interpolate(t) for t in d]
            xs, ys = warp_transform(f"EPSG:{cfg.crs_epsg}", dem.crs,
                                    [p.x for p in pts], [p.y for p in pts])
            v = np.array([s[0] for s in dem.sample(zip(xs, ys))], dtype=float)
            v = v[np.isfinite(v)]
            if dem.nodata is not None:
                v = v[v != dem.nodata]
            return v

        if bed_elevation is None:
            zo = _sample(outline, 200)
            if zo.size == 0:
                raise ValueError("outflow line samples no valid DEM elevations")
            bed_elevation = float(zo.min())             # thalweg
        if slope is None:
            if cfg.geodata.channel_centerline is None:
                raise ValueError("slope not given and no geodata.channel_centerline "
                                 "to derive the reach slope from")
            cl = gpd.read_file(cfg.geodata.channel_centerline)
            if cl.crs and cl.crs.to_epsg() != cfg.crs_epsg:
                cl = cl.to_crs(epsg=cfg.crs_epsg)
            merged = unary_union(cl.geometry.values)
            line = (merged if merged.geom_type == "LineString"
                    else max(merged.geoms, key=lambda g: g.length))
            zc = _sample(line, 400)
            s = np.linspace(0.0, line.length, zc.size)
            slope = abs(float(np.polyfit(s, zc, 1)[0]))

    out = Path(out) if out is not None else Path(cfg.boundaries.stage_discharge)
    rough = f"Strickler Kst={strickler}" if strickler is not None else f"Manning n={manning}"
    log.info("outflow rating from geodata: Q=%.2f m3/s, width=%.1f m (outflow line), "
             "bed=%.2f m (thalweg), slope=%.5f, banks %g:1, %s",
             float(discharge), width, bed_elevation, slope, side_slope, rough)
    return generate_stage_discharge(out, discharge, manning=manning, strickler=strickler,
                                    slope=slope, bottom_width=width, side_slope=side_slope,
                                    bed_elevation=bed_elevation)
