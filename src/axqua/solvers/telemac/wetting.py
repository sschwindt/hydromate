"""Wetted-extent diagnostics for a converged 2D result.

A steady run can balance its boundary fluxes perfectly and still be wrong about
*where* the water is. The two failure modes this module measures are:

**Stagnant film.** A 2D model has neither infiltration nor evaporation, so any water
that ends up somewhere the flow cannot carry it away from stays there for the rest of
the run. That happens when the initial condition seeds a water surface above the one
the run converges to: the excess drains only where it can flow, and on flat ground
over coarse gravel it stops as a one-to-five-centimetre immobile film. The film does
not shrink with a longer run - on isar-2025 it fell by 1.8 % over the last 3100 s -
so it has to be measured, not waited out. :func:`wetting_report` splits the wetted
area into what is *active* (deep and moving), what is *film* (wet but at a standstill)
and what is an *isolated puddle* (wet but not connected to the main water body), and
reports how much of each the initial condition put there.

**A mis-set outflow stage.** A prescribed-elevation outlet that sits above the reach's
own level backs water up over ground that should be dry; one that sits below pulls the
flow into a drawdown and can drive it supercritical. Either shows up as an anomaly in
the free-surface slope over the last few metres before the boundary, which
:func:`outlet_profile` measures against the slope of the reach above it.

Both write a CSV next to the result so successive runs can be compared directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("axqua")

#: below this depth [m] a node counts as dry
WET_DEPTH = 0.01
#: an *active* node is at least this deep [m] ...
ACTIVE_DEPTH = 0.05
#: ... and moving at least this fast [m/s]
ACTIVE_VELOCITY = 0.15
#: a *film* node is wet but slower than this [m/s]
FILM_VELOCITY = 0.05
#: distance bands from the outflow line [m] used by :func:`outlet_profile`
OUTLET_BANDS = ((0.0, 3.0), (3.0, 6.0), (6.0, 10.0), (10.0, 20.0),
                (20.0, 40.0), (40.0, 70.0), (70.0, 120.0))
#: a near-boundary surface slope this many times the reach slope is an anomaly
OUTLET_SLOPE_FACTOR = 1.5


@dataclass
class WettingReport:
    """Where the water sits at the end of a run (see :func:`wetting_report`)."""

    time: float
    wet_area: float
    wet_volume: float
    active_area: float
    active_volume: float
    film_area: float
    film_volume: float
    isolated_area: float
    isolated_volume: float
    isolated_count: int
    #: water an external source holds in place (a groundwater-fed pool on a bar):
    #: legitimately wet, so it is reported on its own and kept out of film/puddles
    supported_area: float = 0.0
    supported_volume: float = 0.0
    seeded_area: float | None = None       # area the initial condition wetted
    seeded_volume: float | None = None
    film_seeded_area: float | None = None  # of the film, how much was seeded
    #: (time, film area) of the sampled frames - a flat tail means it will not drain
    film_history: list[tuple[float, float]] = field(default_factory=list)

    @property
    def film_fraction(self) -> float:
        """Film as a share of the wetted area."""
        return self.film_area / self.wet_area if self.wet_area else 0.0

    @property
    def film_seeded_fraction(self) -> float | None:
        if self.film_seeded_area is None or not self.film_area:
            return None
        return self.film_seeded_area / self.film_area

    @property
    def film_trend(self) -> float | None:
        """Relative change of the film area over the sampled history (negative =
        still draining). ``None`` when fewer than two frames were sampled."""
        if len(self.film_history) < 2:
            return None
        first, last = self.film_history[0][1], self.film_history[-1][1]
        return (last - first) / first if first else None

    def summary(self) -> list[str]:
        """User-facing report lines."""
        lines = [
            (f"wetted {self.wet_area:,.0f} m2 / {self.wet_volume:,.0f} m3 at "
             f"t={self.time:,.0f} s"),
            (f"  active (H>{ACTIVE_DEPTH} m, |U|>{ACTIVE_VELOCITY} m/s): "
             f"{self.active_area:,.0f} m2 / {self.active_volume:,.0f} m3"),
            (f"  stagnant film (|U|<{FILM_VELOCITY} m/s): {self.film_area:,.0f} m2 / "
             f"{self.film_volume:,.1f} m3 ({100 * self.film_fraction:.0f}% of wetted)"),
            (f"  isolated puddles: {self.isolated_count} patches, "
             f"{self.isolated_area:,.0f} m2 / {self.isolated_volume:,.1f} m3"),
        ]
        if self.supported_area:
            lines.append("  water-table pools (externally held, not a defect): "
                         f"{self.supported_area:,.0f} m2 / "
                         f"{self.supported_volume:,.1f} m3")
        if self.seeded_area is not None:
            lines.append(f"  seeded by the initial condition: {self.seeded_area:,.0f} m2 "
                         f"/ {self.seeded_volume:,.0f} m3")
        if (share := self.film_seeded_fraction) is not None:
            lines.append(f"  of the film, {100 * share:.0f}% was seeded "
                         "(the rest the flow spread itself)")
        if (trend := self.film_trend) is not None:
            verdict = ("still draining" if trend < -0.05
                       else "PLATEAUED - a longer run will not remove it")
            lines.append(f"  film over the sampled frames: {100 * trend:+.1f}% "
                         f"({verdict})")
        return lines


def _nodal_areas(x, y, tri):
    """Per-node share of the mesh area [m2] (a third of each incident triangle)."""
    import numpy as np

    xy = np.column_stack([x, y])
    p = xy[tri]
    twice = ((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
             - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
    area = np.zeros(len(x), dtype=float)
    np.add.at(area, tri.ravel(), np.repeat(np.abs(twice) / 6.0, 3))
    return area


def _components(tri, wet, n_nodes):
    """Label the connected components of the wet sub-graph of the mesh."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    edges = np.vstack([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    edges = edges[wet[edges[:, 0]] & wet[edges[:, 1]]]
    adj = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])),
                     shape=(n_nodes, n_nodes))
    _, labels = connected_components(adj + adj.T, directed=False)
    return np.where(wet, labels, -1)


def _fields(results, geometry):
    """Coordinates + element table + the last frame's H, |U| of *results*."""
    import numpy as np

    from axqua.solvers.telemac.sections import _read_fields

    x, y, tri, h, u, v = _read_fields(Path(results), geometry)
    return x, y, tri, h, np.hypot(u, v)


def wetting_report(results, *, geometry=None, initial_conditions=None,
                   supported=None,
                   wet_depth: float = WET_DEPTH, active_depth: float = ACTIVE_DEPTH,
                   active_velocity: float = ACTIVE_VELOCITY,
                   film_velocity: float = FILM_VELOCITY,
                   history_frames: int = 6, out: Path | None = None) -> WettingReport:
    """Split the wetted area of a result into active flow, film and puddles.

    *results* is the run's SELAFIN, *geometry* the build's double-precision geometry
    (its coordinates are used, as in :mod:`axqua.solvers.telemac.sections`), and
    *initial_conditions* the pre-wet SELAFIN, which lets the report attribute the
    film to the seed rather than to the flow. *history_frames* earlier frames are
    re-read to show whether the film is still shrinking; pass 0 to skip that (it is
    the expensive part on a many-frame result).

    *supported* is an optional boolean node mask (or a depth array) marking water an
    external source holds in place - a pool fed by the water table under a gravel bar
    (:mod:`axqua.solvers.telemac.watertable`). Such water is standing and disconnected by
    construction, so without the mask it would be counted as film and as an isolated
    puddle, i.e. reported as a defect when it is exactly what the model intends.

    Writes ``wetting-report.csv`` to *out* (a directory or an explicit file path) when
    given.
    """
    import numpy as np

    from axqua.core.selafin import read_slf

    x, y, tri, h, speed = _fields(results, geometry)
    area = _nodal_areas(x, y, tri)
    wet = h > wet_depth
    active = (h > active_depth) & (speed > active_velocity)
    labels = _components(tri, wet, len(x))
    if wet.any():
        sizes = {c: area[labels == c].sum() for c in np.unique(labels[wet])}
        main = max(sizes, key=sizes.get)
        isolated = wet & (labels != main)
        active &= labels == main       # only the connected body carries the flow
    else:
        isolated = np.zeros_like(wet)
    film = wet & (speed < film_velocity)

    held = np.zeros(len(x), dtype=bool)
    if supported is not None:
        held = np.asarray(supported)
        held = (held > 0) if held.dtype != bool else held
        if held.size != len(x):
            log.warning("supported mask has %d entries but the mesh has %d "
                        "- ignoring it", held.size, len(x))
            held = np.zeros(len(x), dtype=bool)
        else:
            held &= wet
            film &= ~held          # externally held water is not stagnant film ...
            isolated &= ~held      # ... nor an unwanted puddle

    rep = WettingReport(
        time=float(read_slf(Path(results))["time"]),
        wet_area=float(area[wet].sum()), wet_volume=float((h * area)[wet].sum()),
        active_area=float(area[active].sum()),
        active_volume=float((h * area)[active].sum()),
        film_area=float(area[film].sum()), film_volume=float((h * area)[film].sum()),
        isolated_area=float(area[isolated].sum()),
        isolated_volume=float((h * area)[isolated].sum()),
        isolated_count=int(len(np.unique(labels[isolated])) if isolated.any() else 0),
        supported_area=float(area[held].sum()),
        supported_volume=float((h * area)[held].sum()),
    )

    if initial_conditions is not None and Path(initial_conditions).exists():
        ic = read_slf(Path(initial_conditions))["values"]
        key = next((n for n in ic if n.strip().upper() == "WATER DEPTH"), None)
        if key is not None:
            seed = np.asarray(ic[key], dtype=float)
            if seed.size == len(x):
                seeded = seed > 0.0
                rep.seeded_area = float(area[seeded].sum())
                rep.seeded_volume = float((seed * area).sum())
                rep.film_seeded_area = float(area[film & seeded].sum())
            else:
                log.warning("initial conditions have %d nodes but the result has %d "
                            "- not attributing the film to the seed",
                            seed.size, len(x))

    if history_frames:
        rep.film_history = _film_history(results, geometry, area, wet_depth,
                                         film_velocity, history_frames)

    if out is not None:
        _write_report_csv(rep, out)
    return rep


def _film_history(results, geometry, area, wet_depth, film_velocity, frames):
    """Film area at the last *frames* frames, oldest first."""
    import numpy as np

    from axqua.core.selafin import read_slf

    n_times = read_slf(Path(results))["n_times"]
    history = []
    for idx in range(max(0, n_times - frames), n_times):
        try:
            res = read_slf(Path(results), frame=idx)
        except Exception as exc:                     # pragma: no cover - IO guard
            log.debug("film history frame %d unreadable: %s", idx, exc)
            break
        vals = res["values"]
        names = {n.strip().upper(): n for n in vals}
        h = np.asarray(vals[names["WATER DEPTH"]], dtype=float)
        speed = np.hypot(np.asarray(vals[names["VELOCITY U"]], dtype=float),
                         np.asarray(vals[names["VELOCITY V"]], dtype=float))
        film = (h > wet_depth) & (speed < film_velocity)
        history.append((float(res["time"]), float(area[film].sum())))
    return history


def _write_report_csv(rep: WettingReport, out) -> Path:
    import csv

    out = Path(out)
    path = out / "wetting-report.csv" if out.is_dir() else out
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("time_s", rep.time),
        ("wet_area_m2", rep.wet_area), ("wet_volume_m3", rep.wet_volume),
        ("active_area_m2", rep.active_area), ("active_volume_m3", rep.active_volume),
        ("film_area_m2", rep.film_area), ("film_volume_m3", rep.film_volume),
        ("film_fraction", rep.film_fraction),
        ("isolated_area_m2", rep.isolated_area),
        ("isolated_volume_m3", rep.isolated_volume),
        ("isolated_count", rep.isolated_count),
        ("supported_area_m2", rep.supported_area),
        ("supported_volume_m3", rep.supported_volume),
        ("seeded_area_m2", rep.seeded_area), ("seeded_volume_m3", rep.seeded_volume),
        ("film_seeded_area_m2", rep.film_seeded_area),
        ("film_seeded_fraction", rep.film_seeded_fraction),
        ("film_trend", rep.film_trend),
    ]
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["quantity", "value"])
        for name, value in rows:
            writer.writerow([name, "" if value is None else f"{value:.6g}"])
        writer.writerow([])
        writer.writerow(["film_history_time_s", "film_history_area_m2"])
        for t, a in rep.film_history:
            writer.writerow([f"{t:.6g}", f"{a:.6g}"])
    return path


@dataclass
class OutletProfile:
    """Free-surface profile approaching the outflow boundary."""

    bands: list[dict]        # one per distance band, ordered outward from the outlet
    reach_slope: float       # surface slope of the reach above the boundary [-]
    outlet_slope: float      # surface slope over the last band(s) [-]
    verdict: str             # "backwater" | "drawdown" | "neutral"

    def summary(self) -> list[str]:
        lines = [
            (f"outlet profile: {self.verdict} (near-boundary surface slope "
             f"{1000 * self.outlet_slope:.1f} permille vs "
             f"{1000 * self.reach_slope:.1f} permille in the reach above)"),
        ]
        for b in self.bands:
            lines.append(
                f"  {b['from_m']:5.0f}-{b['to_m']:<5.0f} m: WSE {b['wse']:.4f}  "
                f"H {b['depth']:.3f} m  |U| {b['velocity']:.3f} m/s  Fr {b['froude']:.2f}"
            )
        if self.verdict == "backwater":
            lines.append("  -> the prescribed outflow stage sits ABOVE the reach's own "
                         "level: it is holding water back over ground that would "
                         "otherwise be dry. Lower it (or use outflow_condition: free).")
        elif self.verdict == "drawdown":
            lines.append("  -> the flow accelerates into the boundary; the prescribed "
                         "stage is at or slightly below the reach's own level. Check "
                         "the Froude number stays below 1.")
        return lines


def outlet_profile(cfg, results, *, geometry=None, bands=OUTLET_BANDS,
                   out: Path | None = None) -> OutletProfile:
    """Measure the free-surface profile approaching the outflow boundary.

    Bins the **actively flowing** nodes by their distance from the ``outflow`` liquid
    boundary and reports, per band, the discharge-weighted free surface plus the mean
    depth, speed and Froude number. Comparing the surface slope over the last few
    metres with the slope of the reach above it distinguishes

    * **backwater** - the boundary holds the level above the reach's own, the surface
      flattens or rises into it, and the extra depth floods ground near the outlet;
    * **drawdown** - the boundary sits at or below the reach's level and the surface
      steepens into it (mild is normal, especially where the section narrows);
    * **neutral** - the boundary continues the reach profile.

    Writes ``outlet-profile.csv`` to *out* when given.
    """
    import numpy as np
    import shapely

    from axqua.solvers.telemac.boundary import liquid_lines

    lines = liquid_lines(cfg)
    if "outflow" not in lines:
        raise ValueError("outlet_profile needs an 'outflow' line in "
                         "boundaries.liquid_boundaries")
    from axqua.core.selafin import read_slf

    x, y, tri, h, speed = _fields(results, geometry)
    area = _nodal_areas(x, y, tri)
    dist = np.asarray(shapely.distance(shapely.points(x, y), lines["outflow"]))

    # the result carries FREE SURFACE directly; fall back to bottom + depth
    vals = read_slf(Path(results))["values"]
    names = {n.strip().upper(): n for n in vals}
    if "FREE SURFACE" in names:
        surface = np.asarray(vals[names["FREE SURFACE"]], dtype=float)
    elif "BOTTOM" in names:
        surface = np.asarray(vals[names["BOTTOM"]], dtype=float) + h
    else:
        raise ValueError(f"{Path(results).name} has neither FREE SURFACE nor BOTTOM "
                         "(add 'S' or 'B' to VARIABLES FOR GRAPHIC PRINTOUTS)")

    active = (h > ACTIVE_DEPTH) & (speed > ACTIVE_VELOCITY)
    rows = []
    for lo, hi in bands:
        sel = active & (dist >= lo) & (dist < hi)
        if sel.sum() < 10:
            continue
        weight = (h * speed * area)[sel]
        weight = weight / weight.sum() if weight.sum() else None
        wse = float((surface[sel] * weight).sum()) if weight is not None \
            else float(surface[sel].mean())
        froude = speed[sel] / np.sqrt(9.81 * np.maximum(h[sel], 1e-3))
        # the band's lever arm is where the water actually is, not the nominal
        # midpoint: on a partly wet band those differ enough to bias the slope
        rows.append({"from_m": lo, "to_m": hi,
                     "centre_m": float(dist[sel].mean()),
                     "nodes": int(sel.sum()), "area_m2": float(area[sel].sum()),
                     "wse": wse, "depth": float(h[sel].mean()),
                     "velocity": float(speed[sel].mean()),
                     "froude": float(froude.mean())})
    if len(rows) < 3:
        raise ValueError("too few wetted bands near the outflow to profile it "
                         f"(got {len(rows)}); is the outlet wet?")

    def slope_between(a, b):
        run = rows[b]["centre_m"] - rows[a]["centre_m"]
        return (rows[b]["wse"] - rows[a]["wse"]) / run if run else 0.0

    outlet = slope_between(0, 1)
    reach = slope_between(1, len(rows) - 1)
    if outlet < reach / OUTLET_SLOPE_FACTOR:
        verdict = "backwater"
    elif outlet > reach * OUTLET_SLOPE_FACTOR:
        verdict = "drawdown"
    else:
        verdict = "neutral"
    profile = OutletProfile(bands=rows, reach_slope=reach, outlet_slope=outlet,
                            verdict=verdict)
    if out is not None:
        _write_profile_csv(profile, out)
    return profile


def _write_profile_csv(profile: OutletProfile, out) -> Path:
    import csv

    out = Path(out)
    path = out / "outlet-profile.csv" if out.is_dir() else out
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["from_m", "to_m", "centre_m", "nodes", "area_m2", "wse", "depth",
              "velocity", "froude"]
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["# verdict", profile.verdict,
                         "outlet_slope", f"{profile.outlet_slope:.6g}",
                         "reach_slope", f"{profile.reach_slope:.6g}"])
        writer.writerow(fields)
        for row in profile.bands:
            writer.writerow([f"{row[f]:.6g}" if isinstance(row[f], float) else row[f]
                             for f in fields])
    return path
