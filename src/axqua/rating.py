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
The result is written as a ``Q,WSE,depth`` CSV that :func:`axqua.hydraulics.
read_stage_discharge` consumes as ``boundaries.stage_discharge``.
"""

from __future__ import annotations

import csv
import logging
import math
from pathlib import Path
from typing import Iterable

log = logging.getLogger("axqua")


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

    from axqua.boundary import _is_internal, _normalise_kind, _type_column

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
    # only true outflow boundary lines size the section - internal 'int-*' source/
    # sink lines (e.g. 'int-outflow-lose') also contain 'out' and must be excluded,
    # or they inflate the outflow width and corrupt the normal-depth rating.
    outflow = lb[[(not _is_internal(v)) and _normalise_kind(v) == "outflow"
                  for v in lb[type_col]]]
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


def _section_conveyance(station, bed, ks, slope):
    """``Q(WSE)`` of a surveyed cross-section under the fully rough log law.

    Returns a callable; *station*/*bed*/*ks* must already be sorted by station.
    For a trial water level the wetted area and perimeter are integrated over the
    section and the discharge follows from Keulegan, ``C = 18 log10(12 R / ks)`` and
    ``Q = C A sqrt(R S)``.
    """
    import numpy as np

    def q_of(wse: float) -> float:
        h = np.maximum(wse - bed, 0.0)
        if not (h > 0).any():
            return 0.0
        ds = np.gradient(station)
        area = float((h * ds).sum())
        # wetted perimeter along the bed line, not the flat projection
        seg = np.hypot(np.diff(station), np.diff(bed))
        wet_seg = (h[:-1] > 0) | (h[1:] > 0)
        perim = float(seg[wet_seg].sum())
        if area <= 0 or perim <= 0:
            return 0.0
        radius = area / perim
        k = float(np.average(ks, weights=np.where(h > 0, 1.0, 0.0)))
        chezy = 18.0 * math.log10(max(12.0 * radius / max(k, 1e-4), 1.5))
        return chezy * area * math.sqrt(radius * slope)

    return q_of


def _sorted_section(station, bed, ks, slope):
    """Validate and station-sort a cross-section (shared by the stage helpers)."""
    import numpy as np

    station = np.asarray(station, dtype=float)
    bed = np.asarray(bed, dtype=float)
    ks = np.broadcast_to(np.asarray(ks, dtype=float), bed.shape)
    if station.size < 2:
        raise ValueError("a cross-section needs at least two stations")
    if slope <= 0:
        raise ValueError(f"a section stage needs a positive bed slope, got {slope}")
    order = np.argsort(station)
    return station[order], bed[order], ks[order]


def stage_for_discharge(discharge, *, station, bed, ks, slope, freeboard=10.0):
    """Water level [m] at which a **surveyed cross-section** conveys *discharge*.

    Inverts the section conveyance (:func:`_section_conveyance`) by bisection between
    the section thalweg and its highest point plus *freeboard*. This is the normal
    (uniform-flow) stage of the section: the level at which the friction slope equals
    the bed *slope*.

    Used both by :func:`section_rating` (one stage per requested discharge, written to
    a rating CSV) and by the pre-wet initial condition, which needs the normal stage of
    a whole sequence of cross-sections along the reach - see
    :func:`axqua.steering._normal_depth_prewet_surface`.
    """
    station, bed, ks = _sorted_section(station, bed, ks, slope)
    q_of = _section_conveyance(station, bed, ks, slope)
    lo, hi = float(bed.min()), float(bed.max()) + float(freeboard)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if q_of(mid) < discharge:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def section_rating(out, discharges, *, station, bed, ks, slope, log_label=""):
    """Stage-discharge curve from a **surveyed cross-section** rather than a trapezoid.

    ``station``/``bed`` are the along-section distance (m) and bed elevation (m) of
    the real section; ``ks`` is the Nikuradse roughness (m), a scalar or one value
    per station. Each requested discharge is turned into a stage by
    :func:`stage_for_discharge`.

    A trapezoidal idealisation of the same section (:func:`generate_stage_discharge`)
    puts the full width at the full depth, so it over-estimates the flow area at a
    given stage and returns a stage that is too **low**. On a V- or U-shaped natural
    section the error is large enough to force a supercritical drawdown at a
    prescribed-elevation outflow - the boundary pulls the level below the reach's
    own normal depth and the flow accelerates through the outlet to satisfy
    continuity. Integrating the real section removes that artefact.
    """
    station, bed, ks = _sorted_section(station, bed, ks, slope)

    qs = [float(discharges)] if isinstance(discharges, (int, float)) else \
        sorted(float(q) for q in discharges)
    rows = []
    lo0 = float(bed.min())
    for q in qs:
        wse = stage_for_discharge(q, station=station, bed=bed, ks=ks, slope=slope)
        rows.append((q, wse, wse - lo0))
        log.info("outflow rating %s: Q=%.3f m3/s -> WSE=%.4f m "
                 "(%.3f m over the section thalweg %.3f m)",
                 log_label, q, wse, wse - lo0, lo0)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Q", "WSE", "depth"])
        for q, wse, h in rows:
            writer.writerow([f"{q:.4f}", f"{wse:.4f}", f"{h:.4f}"])
    return out


def synthesize_outflow_rating_from_section(cfg, discharge, *, slope=None, out=None,
                                           n_samples: int = 300):
    """Outflow rating from the DEM cross-section along the outflow boundary line.

    Samples the bed along the ``outflow`` liquid-boundary line straight out of
    ``geodata.dem_initial``, picks up the Nikuradse ``ks`` of the roughness zone each
    sample falls in (``geodata.roughness_zones`` + ``roughness_table``; the lateral
    ``friction.boundary_*`` roughness otherwise), takes the reach slope from a linear
    fit of the DEM along ``geodata.channel_centerline``, and inverts the section
    conveyance with :func:`section_rating`.
    """
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.warp import transform as warp_transform
    from shapely.ops import unary_union

    from axqua.boundary import _is_internal, _normalise_kind, _type_column

    lb = gpd.read_file(cfg.boundaries.liquid_boundaries)
    if lb.crs and lb.crs.to_epsg() != cfg.crs_epsg:
        lb = lb.to_crs(epsg=cfg.crs_epsg)
    type_col = _type_column(lb)
    outflow = lb[[(not _is_internal(v)) and _normalise_kind(v) == "outflow"
                  for v in lb[type_col]]]
    if outflow.empty:
        raise ValueError("no 'outflow' line in boundaries.liquid_boundaries")
    line = unary_union(outflow.geometry.values)
    if line.geom_type != "LineString":
        line = max(line.geoms, key=lambda g: g.length)

    station = np.linspace(0.0, line.length, n_samples)
    pts = [line.interpolate(t) for t in station]
    with rasterio.open(cfg.geodata.dem_initial) as dem:
        xs, ys = warp_transform(f"EPSG:{cfg.crs_epsg}", dem.crs,
                                [p.x for p in pts], [p.y for p in pts])
        bed = np.array([s[0] for s in dem.sample(zip(xs, ys))], dtype=float)
        if slope is None:
            cl = gpd.read_file(cfg.geodata.channel_centerline)
            if cl.crs and cl.crs.to_epsg() != cfg.crs_epsg:
                cl = cl.to_crs(epsg=cfg.crs_epsg)
            merged = unary_union(cl.geometry.values)
            cline = (merged if merged.geom_type == "LineString"
                     else max(merged.geoms, key=lambda g: g.length))
            sc = np.linspace(0.0, cline.length, 400)
            cp = [cline.interpolate(t) for t in sc]
            cx, cy = warp_transform(f"EPSG:{cfg.crs_epsg}", dem.crs,
                                    [p.x for p in cp], [p.y for p in cp])
            zc = np.array([s[0] for s in dem.sample(zip(cx, cy))], dtype=float)
            good = np.isfinite(zc)
            slope = abs(float(np.polyfit(sc[good], zc[good], 1)[0]))
    good = np.isfinite(bed)
    if dem_nodata := getattr(dem, "nodata", None):
        good &= bed != dem_nodata
    station, bed, pts = station[good], bed[good], [p for p, g in zip(pts, good) if g]

    # roughness per sample from the roughness zones, else the lateral-boundary value
    ks = None
    if cfg.geodata.roughness_zones is not None and cfg.geodata.roughness_table is not None:
        import pandas as pd
        zones = gpd.read_file(cfg.geodata.roughness_zones)
        if zones.crs and zones.crs.to_epsg() != cfg.crs_epsg:
            zones = zones.to_crs(epsg=cfg.crs_epsg)
        table = pd.read_csv(cfg.geodata.roughness_table)
        lookup = dict(zip(table.iloc[:, 0].astype(int), table.iloc[:, 1].astype(float)))
        zid_col = next((c for c in zones.columns if c.lower().replace(" ", "_")
                        in ("zone_id", "zoneid", "id")), None)
        if zid_col is not None:
            vals = []
            for p in pts:
                hit = zones[zones.contains(p)]
                if hit.empty:
                    hit = zones.iloc[[zones.distance(p).idxmin()]]
                vals.append(lookup.get(int(hit.iloc[0][zid_col]), float("nan")))
            ks = np.array(vals, dtype=float)
            if not np.isfinite(ks).all():
                ks = np.where(np.isfinite(ks), ks, np.nanmean(ks))
    if ks is None:
        law, coef = cfg.friction.boundary_law, cfg.friction.boundary_coefficient
        n = _resolve_n(coef if law == 4 else None, coef if law == 3 else None)
        ks = np.full(bed.shape, (n / 0.0474) ** 6)   # Strickler-Manning -> ks (m)
        log.info("no roughness zones for the outflow section: using ks=%.3f m "
                 "from friction.boundary_*", float(ks[0]))

    out = Path(out) if out is not None else Path(cfg.boundaries.stage_discharge)
    log.info("outflow section: %.2f m wide, bed %.3f..%.3f m, ks %.3f..%.3f m, "
             "reach slope %.5f", float(station.max()), float(bed.min()),
             float(bed.max()), float(np.min(ks)), float(np.max(ks)), slope)
    return section_rating(out, discharge, station=station, bed=bed, ks=ks, slope=slope,
                          log_label="(DEM cross-section)")
