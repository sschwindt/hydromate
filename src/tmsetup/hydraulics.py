"""Readers for hydraulic inputs: inflow, stage-discharge, and measurements.

Handles the Bavarian LfU (gkd.bayern.de) CSV export format (UTF-8 BOM,
``;``-separated, comma decimals, ~10 metadata lines, then a ``Datum;...`` header)
as well as plain 2-column CSVs, so the workflow ingests both the raw gauge files
in ``stage-discharge/`` and tidy user-prepared tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


def _is_lfu(path: Path) -> bool:
    with open(path, encoding="utf-8-sig", errors="ignore") as fh:
        head = fh.read(400)
    return "gkd.bayern" in head or "Messstellen-Nr" in head or "Datum;" in head


def _read_lfu(path: Path) -> pd.DataFrame:
    """Read an LfU time series -> DataFrame[datetime, value]."""
    with open(path, encoding="utf-8-sig", errors="ignore") as fh:
        lines = fh.readlines()
    header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Datum;"))
    df = pd.read_csv(
        path, sep=";", decimal=",", skiprows=header_idx,
        encoding="utf-8-sig", engine="python",
    )
    df = df.iloc[:, :2]
    df.columns = ["datetime", "value"]
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna()


def _read_generic(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.shape[1] < 2:
        df = pd.read_csv(path, sep=";", decimal=",")
    return df


@dataclass
class Inflow:
    times_s: np.ndarray | None      # seconds from start (unsteady), else None
    discharge: np.ndarray           # m3/s series, or single-element array
    steady_value: float             # representative steady discharge (mean)


def read_inflow(path: Path, steady: bool = True) -> Inflow:
    """Read an inflow discharge series (LfU or generic) into an :class:`Inflow`."""
    path = Path(path)
    if _is_lfu(path):
        df = _read_lfu(path)
        q = df["value"].to_numpy(dtype=float)
        t = (df["datetime"] - df["datetime"].iloc[0]).dt.total_seconds().to_numpy()
    else:
        df = _read_generic(path)
        cols = {c.lower(): c for c in df.columns}
        qcol = next((cols[c] for c in cols if "q" in c or "disch" in c or "abfluss" in c),
                    df.columns[-1])
        q = pd.to_numeric(df[qcol], errors="coerce").dropna().to_numpy(dtype=float)
        t = np.arange(len(q), dtype=float) if len(q) > 1 else None
    steady_value = float(np.mean(q))
    return Inflow(times_s=(None if steady else t), discharge=q, steady_value=steady_value)


def read_stage_discharge(path: Path):
    """Read a rating curve -> callable mapping discharge (m3/s) to WSE (m).

    Expects two numeric columns; the one whose values look like discharges
    (smaller magnitude, m3/s) is treated as Q and the other as stage/elevation.
    """
    df = _read_generic(Path(path))
    num = df.select_dtypes("number")
    if num.shape[1] < 2:
        num = df.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    cols = {c.lower(): c for c in df.columns}
    qcol = next((cols[c] for c in cols if "q" in c or "disch" in c or "abfluss" in c), None)
    hcol = next((cols[c] for c in cols if "stage" in c or "wse" in c or "elev"
                 in c or "wasserstand" in c or "h" == c.lower()), None)
    if qcol is None or hcol is None:
        qcol, hcol = num.columns[0], num.columns[1]
    q = pd.to_numeric(df[qcol], errors="coerce").to_numpy(dtype=float)
    h = pd.to_numeric(df[hcol], errors="coerce").to_numpy(dtype=float)
    order = np.argsort(q)
    q, h = q[order], h[order]

    def wse_at(discharge: float) -> float:
        return float(np.interp(discharge, q, h))

    return wse_at


def read_measurements(path: Path, crs_epsg: int) -> pd.DataFrame:
    """Read hydraulic measurement points into a tidy DataFrame.

    Returns columns: id, x, y, z (vertical offset, default 0), and any of
    ``water_depth`` / ``scalar_velocity`` that are present. Accepts a point
    shapefile/gpkg or a CSV with x/y columns.
    """
    path = Path(path)
    if path.suffix.lower() in (".shp", ".gpkg", ".geojson"):
        import geopandas as gpd

        gdf = gpd.read_file(path)
        if gdf.crs and gdf.crs.to_epsg() != crs_epsg:
            gdf = gdf.to_crs(epsg=crs_epsg)
        df = pd.DataFrame(gdf.drop(columns="geometry"))
        df["x"] = gdf.geometry.x.to_numpy()
        df["y"] = gdf.geometry.y.to_numpy()
    else:
        df = _read_generic(path)

    lower = {c.lower(): c for c in df.columns}

    def pick(*names, default=None):
        for n in names:
            if n in lower:
                return df[lower[n]]
        return default

    out = pd.DataFrame()
    pid = pick("id", "name", "point")
    out["id"] = pid if pid is not None else np.arange(1, len(df) + 1)
    out["x"] = pd.to_numeric(pick("x", "easting", "ostwert"), errors="coerce")
    out["y"] = pd.to_numeric(pick("y", "northing", "nordwert"), errors="coerce")
    zoff = pick("z", "z_offset", "depth_below_surface")
    out["z"] = pd.to_numeric(zoff, errors="coerce") if zoff is not None else 0.0
    depth = pick("water_depth", "water depth", "depth", "h", "wd")
    if depth is not None:
        out["water_depth"] = pd.to_numeric(depth, errors="coerce")
    vel = pick("scalar_velocity", "scalar velocity", "velocity", "v", "speed")
    if vel is not None:
        out["scalar_velocity"] = pd.to_numeric(vel, errors="coerce")
    return out.dropna(subset=["x", "y"]).reset_index(drop=True)
