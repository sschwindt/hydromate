"""Multi-campaign FlowTracker -> per-flow HydroBayesCal calibration-point CSVs.

A **multi-discharge** calibration compares one model against velocity ground
truth from several field campaigns, each measured at a different steady
discharge. This module turns each campaign's FlowTracker2 export into a
calibration-points CSV in the HydroBayesCal schema (first four columns
``id, x, y, z`` read by position, then ``<QTY>_DATA`` / ``<QTY>_ERROR`` read by
name).

Per vertical it keeps the measurement nearest **0.6 x depth** (the standard
depth-averaged-velocity proxy that TELEMAC-2D ``SCALAR VELOCITY`` =
sqrt(u^2+v^2) is compared to) and forms the target from the **horizontal**
components only. ``WATER DEPTH`` (the vertical's total depth) is carried
alongside.

Three source layouts are handled (``kind`` on :class:`~axqua.config`-level
flow specs, or the ``compile_*`` helpers directly):

* ``adapter`` - a revised summary workbook whose sheet already has one row per
  vertical, joined to a DGPS point layer by ID (via
  :func:`axqua.ground_truth.read_flowtracker`); the standard axqua path.
* ``transect`` - a taped cross-section whose ``Station`` is ``<vertical>-<sub>``
  with 0.2/0.6/0.8 sub-verticals and coordinates only on bank-anchor rows;
  positions come from a companion point GeoPackage keyed by vertical number.
* ``inline`` - verticals with EPSG coordinates repeated inline on every row and
  0.2/0.6/0.8 profiles.

The per-point velocity error propagates the FlowTracker component errors and is
floored (``vel_err_floor``). Raise the floor for a campaign that is not an
independent representative sample of the 2D flow field (e.g. one dense
cross-section), so it informs but does not dominate the joint likelihood
(HydroBayesCal weights each point by 1/variance).
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from axqua.ground_truth import read_flowtracker, read_xlsx_sheet

VELOCITY_ERROR_FLOOR = 0.01   # m/s (instrument floor)
DEPTH_ERROR_FLOOR = 0.02      # m  (DGPS/staff depth precision)


# --------------------------------------------------------------------------- #
# shared assembly
# --------------------------------------------------------------------------- #
def velocity_and_error(u, v, u_err, v_err, vel_err_floor=VELOCITY_ERROR_FLOOR):
    """Horizontal speed sqrt(u^2+v^2) and propagated per-point error (floored).

    ``sigma_V = sqrt((u*su)^2 + (v*sv)^2) / V``, then clipped up to
    ``vel_err_floor``.
    """
    u, v = np.asarray(u, float), np.asarray(v, float)
    su = np.nan_to_num(np.asarray(u_err, float))
    sv = np.nan_to_num(np.asarray(v_err, float))
    vel = np.hypot(u, v)
    with np.errstate(divide="ignore", invalid="ignore"):
        err = np.sqrt((u * su) ** 2 + (v * sv) ** 2) / np.where(vel > 0, vel, np.nan)
    return vel, np.clip(np.nan_to_num(err), vel_err_floor, None)


def assemble_velocity_csv(x, y, z, u, v, u_err, v_err, h, labels,
                          vel_err_floor=VELOCITY_ERROR_FLOOR,
                          depth_err_floor=DEPTH_ERROR_FLOOR) -> pd.DataFrame:
    """Build the HydroBayesCal calibration-points frame from per-vertical arrays."""
    vel, err = velocity_and_error(u, v, u_err, v_err, vel_err_floor)
    n = len(x)
    return pd.DataFrame({
        "id": np.arange(1, n + 1),
        "x": np.round(np.asarray(x, float), 3),
        "y": np.round(np.asarray(y, float), 3),
        "z": np.round(np.asarray(z, float), 3),
        "SCALAR VELOCITY_DATA": np.round(vel, 6),
        "SCALAR VELOCITY_ERROR": np.round(err, 6),
        "WATER DEPTH_DATA": np.round(np.asarray(h, float), 6),
        "WATER DEPTH_ERROR": np.full(n, depth_err_floor),
        "label": list(labels),
    })


def _pick_relative_depth(profile: list[dict], target: float = 0.6) -> dict:
    """The profile sub-vertical whose relative depth is nearest ``target``."""
    return min(profile, key=lambda r: abs(r["pct"] - target))


def _num(row, col):
    try:
        return float(row.iloc[col])
    except (TypeError, ValueError):
        return np.nan


def _header_map(raw: pd.DataFrame) -> tuple[int, dict[str, int]]:
    """Locate the header row (first cell 'Point') and map labels -> columns."""
    for i in range(min(len(raw), 10)):
        if str(raw.iloc[i, 0]).strip() == "Point":
            return i, {str(cell).strip(): j
                       for j, cell in enumerate(raw.iloc[i]) if cell is not None}
    raise ValueError("no 'Point' header row found in the campaign sheet")


# --------------------------------------------------------------------------- #
# layout compilers
# --------------------------------------------------------------------------- #
def compile_adapter(values_xlsx: Path, positions, crs_epsg: int = 25832,
                    vel_err_floor: float = VELOCITY_ERROR_FLOOR,
                    label: str = "pt") -> pd.DataFrame:
    """Revised summary workbook joined to a DGPS layer by ID (standard path)."""
    tidy = read_flowtracker(Path(values_xlsx), Path(positions), crs_epsg)
    return assemble_velocity_csv(
        tidy["x"], tidy["y"], tidy["z"], tidy["u"], tidy["v"],
        tidy.get("u_err", 0.0), tidy.get("v_err", 0.0), tidy["h"],
        [f"{label}-{i + 1}" for i in range(len(tidy))], vel_err_floor)


def compile_transect(values_xlsx: Path, positions_gpkg: Path, sheet: str = "KB15",
                     vel_err_floor: float = VELOCITY_ERROR_FLOOR,
                     target_depth: float = 0.6, label: str = "tr") -> pd.DataFrame:
    """Taped cross-section: values from the xlsx, positions from a point gpkg.

    ``Station`` is ``<vertical>-<sub>``; positions come from ``positions_gpkg``
    whose ``Point`` attribute is the (integer) vertical number.
    """
    import geopandas as gpd

    raw = read_xlsx_sheet(Path(values_xlsx), sheet=sheet)
    hidx, cols = _header_map(raw)
    need = ("Point", "Station", "% Depth", "Total Depth [m]", "v(x) [m/s]",
            "v(y) [m/s]", "v_err(x) [m/s]", "v_err(y) [m/s]")
    c = {k: cols[k] for k in need}

    verticals: dict[int, list[dict]] = {}
    for i in range(hidx + 1, len(raw)):
        row = raw.iloc[i]
        vx, pct = _num(row, c["v(x) [m/s]"]), _num(row, c["% Depth"])
        station = str(row.iloc[c["Station"]] or "").strip()
        m = re.match(r"(\d+)-(\d+)$", station)
        if np.isnan(vx) or np.isnan(pct) or not m:
            continue                     # bank-anchor / blank rows
        verticals.setdefault(int(m.group(1)), []).append(dict(
            pct=pct, h=_num(row, c["Total Depth [m]"]),
            u=vx, v=_num(row, c["v(y) [m/s]"]),
            u_err=_num(row, c["v_err(x) [m/s]"]), v_err=_num(row, c["v_err(y) [m/s]"])))
    if not verticals:
        raise ValueError(f"no measurement verticals in {Path(values_xlsx).name} [{sheet}]")

    pts = gpd.read_file(Path(positions_gpkg))
    pos: dict[int, tuple] = {}
    for _, p in pts.iterrows():
        try:
            n = int(str(p["Point"]).strip())
        except ValueError:
            continue
        pos.setdefault(n, (p.geometry.x, p.geometry.y,
                           float(p["Alt."]) if "Alt." in pts.columns else p.geometry.z))
    missing = sorted(set(verticals) - set(pos))
    if missing:
        raise ValueError(f"{Path(positions_gpkg).name}: verticals {missing} have no position")

    rows = {k: _pick_relative_depth(v, target_depth) for k, v in sorted(verticals.items())}
    return assemble_velocity_csv(
        [pos[k][0] for k in rows], [pos[k][1] for k in rows], [pos[k][2] for k in rows],
        [r["u"] for r in rows.values()], [r["v"] for r in rows.values()],
        [r["u_err"] for r in rows.values()], [r["v_err"] for r in rows.values()],
        [r["h"] for r in rows.values()], [f"{label}-V{k}" for k in rows], vel_err_floor)


def compile_inline(values_xlsx: Path, sheet: str = "KB15",
                   vel_err_floor: float = VELOCITY_ERROR_FLOOR,
                   target_depth: float = 0.6, label: str = "in") -> pd.DataFrame:
    """Verticals with inline EPSG coordinates on every row and 0.2/0.6/0.8 profiles."""
    raw = read_xlsx_sheet(Path(values_xlsx), sheet=sheet)
    hidx, cols = _header_map(raw)
    need = ("Point", "East.", "North.", "Alt.", "% Depth", "Total Depth [m]",
            "v(x) [m/s]", "v(y) [m/s]", "v_err(x) [m/s]", "v_err(y) [m/s]")
    c = {k: cols[k] for k in need}

    groups: dict[str, dict] = {}
    current = None
    for i in range(hidx + 1, len(raw)):
        row = raw.iloc[i]
        vx, pct = _num(row, c["v(x) [m/s]"]), _num(row, c["% Depth"])
        point = str(row.iloc[c["Point"]] or "").strip()
        if point and point.lower() not in ("nan", "none"):
            current = point
        if np.isnan(vx) or np.isnan(pct) or current is None:
            continue
        g = groups.setdefault(current, dict(profile=[], x=np.nan, y=np.nan, z=np.nan))
        east = _num(row, c["East."])
        if not np.isnan(east):
            g["x"], g["y"], g["z"] = east, _num(row, c["North."]), _num(row, c["Alt."])
        g["profile"].append(dict(
            pct=pct, h=_num(row, c["Total Depth [m]"]),
            u=vx, v=_num(row, c["v(y) [m/s]"]),
            u_err=_num(row, c["v_err(x) [m/s]"]), v_err=_num(row, c["v_err(y) [m/s]"])))
    if not groups:
        raise ValueError(f"no measurement verticals in {Path(values_xlsx).name} [{sheet}]")
    bad = [k for k, g in groups.items() if np.isnan(g["x"])]
    if bad:
        raise ValueError(f"{Path(values_xlsx).name}: verticals {bad} carry no inline coordinates")

    rows = {k: _pick_relative_depth(g["profile"], target_depth) for k, g in groups.items()}
    return assemble_velocity_csv(
        [groups[k]["x"] for k in rows], [groups[k]["y"] for k in rows],
        [groups[k]["z"] for k in rows],
        [r["u"] for r in rows.values()], [r["v"] for r in rows.values()],
        [r["u_err"] for r in rows.values()], [r["v_err"] for r in rows.values()],
        [r["h"] for r in rows.values()],
        [f"{label}-{k.replace(' ', '')}" for k in rows], vel_err_floor)


_COMPILERS = {"adapter": compile_adapter, "transect": compile_transect,
              "inline": compile_inline}


def compile_campaign(kind: str, **kwargs) -> pd.DataFrame:
    """Dispatch to the ``adapter`` / ``transect`` / ``inline`` layout compiler."""
    try:
        fn = _COMPILERS[kind]
    except KeyError:
        raise ValueError(f"unknown campaign layout {kind!r} "
                         f"(choose {sorted(_COMPILERS)})") from None
    return fn(**kwargs)
