"""Compile the Wallgau/upper Isar HOBO logger xlsx exports into a point GeoPackage.

Output layers (EPSG:25832):
  water-temperature : long-format time series, one point feature per record
                      (stone, position oben/unten, serial, datetime, temp_c, lux)
  logger-locations  : one point per stone with serials, elevation and summary stats
"""

import glob
import os
import re

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

BASE = "/home/schwindt/github/axqua/cases/isar-2025/user-sources/water-temperature"
TS_DIR = os.path.join(BASE, "time-series")
LOC_FILE = os.path.join(BASE, "location", "logger-location.txt")
OUT = "/home/schwindt/github/axqua/cases/isar-2025/user-sources/geodata/water-temperature.gpkg"
CRS = "EPSG:25832"


def norm_stone(raw: str) -> str:
    """'L-S8' -> 'S08', 'L-D2' -> 'D02', 'L-S15' -> 'S15'."""
    m = re.match(r"L-([SD])(\d+)$", raw.strip())
    if not m:
        raise ValueError(f"unrecognised logger name: {raw!r}")
    return f"{m.group(1)}{int(m.group(2)):02d}"


def read_locations() -> pd.DataFrame:
    loc = pd.read_csv(
        LOC_FILE, sep="\t", header=None,
        names=["name", "x", "y", "z"],
    )
    loc = loc[loc["name"].astype(str).str.startswith("L-")].dropna(subset=["x", "y"])
    loc["stone"] = loc["name"].map(norm_stone)
    return loc.set_index("stone")


def read_series() -> pd.DataFrame:
    frames = []
    pattern = re.compile(r"^([SD]\d{2})_([OU])-(\d+)\s")
    for path in sorted(glob.glob(os.path.join(TS_DIR, "[SD]*.xlsx"))):
        fname = os.path.basename(path)
        m = pattern.match(fname)
        if not m:
            print(f"  skipping (no stone/serial in name): {fname}")
            continue
        stone, pos, serial = m.group(1), m.group(2), m.group(3)
        df = pd.read_excel(path, sheet_name="Data")
        df = df.rename(columns={
            "Date-Time (CET/CEST)": "datetime",
            "Temperature , °C": "temp_c",
            "Light , lux": "lux",
        })
        keep = ["datetime", "temp_c"] + (["lux"] if "lux" in df.columns else [])
        df = df[keep].copy()
        if "lux" not in df.columns:
            df["lux"] = pd.NA
        df["stone"] = stone
        df["position"] = "oben" if pos == "O" else "unten"
        df["serial"] = serial
        frames.append(df)
        print(f"  {fname.split(' ')[0]}: {len(df)} records"
              f"{' (with lux)' if df['lux'].notna().any() else ''}")
    return pd.concat(frames, ignore_index=True)


def main():
    loc = read_locations()
    print(f"{len(loc)} logger locations read")
    ts = read_series()
    print(f"{len(ts)} time-series records from {ts['serial'].nunique()} loggers")

    missing = sorted(set(ts["stone"]) - set(loc.index))
    if missing:
        raise SystemExit(f"stones without coordinates: {missing}")
    unused = sorted(set(loc.index) - set(ts["stone"]))
    if unused:
        print(f"locations without time series: {unused}")

    ts = ts.join(loc[["x", "y", "z"]], on="stone")
    ts["lux"] = pd.to_numeric(ts["lux"], errors="coerce")
    ts = ts[["stone", "position", "serial", "datetime", "temp_c", "lux", "z"]
            ].rename(columns={"z": "elevation_m"}).assign(
        geometry=[Point(xy) for xy in zip(
            ts["x"], ts["y"])])
    gdf = gpd.GeoDataFrame(ts, geometry="geometry", crs=CRS)

    # summary layer: one point per stone
    grp = gdf.groupby(["stone", "position"])
    stats = grp.agg(
        serial=("serial", "first"),
        t_start=("datetime", "min"),
        t_end=("datetime", "max"),
        n_records=("temp_c", "size"),
        temp_mean=("temp_c", "mean"),
        temp_min=("temp_c", "min"),
        temp_max=("temp_c", "max"),
        lux_mean=("lux", "mean"),
        lux_max=("lux", "max"),
    ).reset_index()
    wide = stats.pivot(index="stone", columns="position")
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.join(loc[["name", "x", "y", "z"]]).reset_index()
    locs = gpd.GeoDataFrame(
        wide.rename(columns={"name": "name_raw", "z": "elevation_m"}),
        geometry=[Point(xy) for xy in zip(wide["x"], wide["y"])], crs=CRS,
    ).drop(columns=["x", "y"])
    for c in locs.columns:
        if c.startswith(("t_start", "t_end")):
            locs[c] = locs[c].astype(str)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    gdf.to_file(OUT, layer="water-temperature", driver="GPKG")
    locs.to_file(OUT, layer="logger-locations", driver="GPKG", mode="a")
    print(f"written: {OUT}")
    print(f"  layer water-temperature: {len(gdf)} features")
    print(f"  layer logger-locations : {len(locs)} features")


if __name__ == "__main__":
    main()
