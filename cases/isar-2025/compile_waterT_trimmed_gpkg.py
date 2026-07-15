"""Compile waterT-trimmed.gpkg: HOBO logger data trimmed to the in-river windows.

The raw record (see compile_water_temperature_gpkg.py) shows the loggers were in
the upper Isar only during two campaigns; the rest is indoor storage (~14-23 °C,
no daylight lux) plus transport nights. Deployed episodes are detected per logger:
contiguous runs where the 30-min rolling median temperature stays < 15 °C, that
contain water-cold samples (< 8 °C), last >= 12 h, and are either river-cold
(min < 5.5 °C) or long (>= 48 h). This keeps the two river campaigns
(~2025-02-15..21 and ~2025-03-16..20, incl. per-logger deviations such as S67's
late single-night entry in February) and rejects the Feb 13-14 transport nights
(min >= 6.4 °C) and the ~7 h Mar 5 site check.

Two campaign-specific refinements, both verified against the cross-logger panel:
- The night of Mar 19-20 was a foehn night: EVERY logger warmed in lockstep to
  ~12-16 °C and cooled back to ~8-9 °C when the front arrived on the Mar 20
  morning - genuine water temperature, kept. Episodes split by a brief > 15 °C
  foehn peak (S27) are re-merged across gaps < 6 h whose temperature stays
  < 16.5 °C; otherwise a 1 h handling buffer is shaved off both episode ends.
- In February the river never exceeded 6.6 °C, but after the Feb 21 morning
  retrieval several loggers kept logging ~13-15 °C in storage bags (S12 for a
  full extra day), which the 15 °C cutoff cannot see. February episodes (start
  before Mar 1) are therefore trimmed to their sub-8 °C water band (first/last
  30-min-median crossing), which also pins the Feb 15 immersion times.

Output layers (EPSG:25832), analogous to water-temperature.gpkg plus `campaign`:
  water-temperature : trimmed long-format time series points
  logger-locations  : one point per stone with deployed-window summary stats
"""

import glob
import os
import re

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "user-sources", "water-temperature")
TS_DIR = os.path.join(BASE, "time-series")
LOC_FILE = os.path.join(BASE, "location", "logger-location.txt")
OUT = os.path.join(os.path.dirname(BASE), "geodata", "waterT-trimmed.gpkg")
CRS = "EPSG:25832"

INDOOR_T = 15.0        # deg C: indoor storage never cools below ~13.6; river never warms above ~13.5
WATER_T = 8.0          # deg C: every genuine episode contains sub-8 water
RIVER_COLD_T = 5.5     # deg C: river nights reach < 5.5; transport nights stay >= 6.4
MIN_DURATION_H = 12.0  # rejects the ~7 h Mar 5 site check
LONG_DURATION_H = 48.0 # multi-day episodes count even if their min stays >= 5.5 (e.g. S08)
EDGE_BUFFER = pd.Timedelta("1h")  # handling buffer shaved off both episode ends
MERGE_GAP = pd.Timedelta("6h")    # re-merge episodes split by a warm blip (foehn peak)
MERGE_MAX_T = 16.0     # deg C: the splitting blip must stay below this to merge
                       # (S27's foehn peak ~15.5 merges; S67_U's 18 degC handling
                       #  spike at the Mar 19 retrieval does not)
DESPIKE_T = 2.0        # deg C: drop samples deviating more than this from the 30-min
                       # median - water cannot jump that fast; air/hand contact can
WINTER_SPLIT = pd.Timestamp("2025-03-01")  # episodes starting before this: Feb campaign
WINTER_WATER_T = 8.0   # deg C: Feb river water stayed < 6.6; > 8 is handling/storage


def norm_stone(raw: str) -> str:
    """'L-S8' -> 'S08', 'L-D2' -> 'D02', 'L-S15' -> 'S15'."""
    m = re.match(r"L-([SD])(\d+)$", raw.strip())
    if not m:
        raise ValueError(f"unrecognised logger name: {raw!r}")
    return f"{m.group(1)}{int(m.group(2)):02d}"


def read_locations() -> pd.DataFrame:
    loc = pd.read_csv(LOC_FILE, sep="\t", header=None, names=["name", "x", "y", "z"])
    loc = loc[loc["name"].astype(str).str.startswith("L-")].dropna(subset=["x", "y"])
    loc["stone"] = loc["name"].map(norm_stone)
    return loc.set_index("stone")


def deployed_episodes(s: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Detect in-river windows from a datetime-indexed temperature series."""
    sm = s.rolling("30min").median()
    mask = sm < INDOOR_T
    runs = (mask != mask.shift()).cumsum()
    raw = [(chunk.index[0], chunk.index[-1]) for _, chunk in sm[mask].groupby(runs[mask])]

    # re-merge runs split by a short, still-cool blip (the Mar 19-20 foehn peak)
    merged = []
    for t0, t1 in raw:
        if merged and t0 - merged[-1][1] < MERGE_GAP \
                and sm.loc[merged[-1][1]:t0].max() < MERGE_MAX_T:
            merged[-1] = (merged[-1][0], t1)
        else:
            merged.append((t0, t1))

    episodes = []
    for t0, t1 in merged:
        chunk = sm.loc[t0:t1]
        dur_h = (t1 - t0).total_seconds() / 3600
        if dur_h < MIN_DURATION_H or chunk.min() >= WATER_T:
            continue
        if chunk.min() >= RIVER_COLD_T and dur_h < LONG_DURATION_H:
            continue  # transport night, not river-cold and not multi-day
        if t0 < WINTER_SPLIT:
            # Feb campaign: water stayed < 6.6 degC; clip to the sub-8 water band
            wet = chunk[chunk < WINTER_WATER_T]
            if wet.empty:
                continue
            episodes.append((wet.index[0], wet.index[-1]))
        else:
            episodes.append((t0 + EDGE_BUFFER, t1 - EDGE_BUFFER))
    return episodes


def campaign_label(t: pd.Timestamp) -> str:
    return t.strftime("%b-%Y").lower()


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
        df = pd.read_excel(path, sheet_name="Data").rename(columns={
            "Date-Time (CET/CEST)": "datetime",
            "Temperature , °C": "temp_c",
            "Light , lux": "lux",
        })
        df["datetime"] = pd.to_datetime(df["datetime"])
        if "lux" not in df.columns:
            df["lux"] = pd.NA
        s = df.set_index("datetime")["temp_c"]
        sm = s.rolling("30min").median()
        df["spike"] = (s - sm).abs().values > DESPIKE_T
        parts = []
        for t0, t1 in deployed_episodes(s):
            ep = df[(df["datetime"] >= t0) & (df["datetime"] <= t1) & ~df["spike"]].copy()
            if t0 < WINTER_SPLIT:
                ep = ep[ep["temp_c"] < WINTER_WATER_T]
            ep = ep.drop(columns="spike")
            ep["campaign"] = campaign_label(t0)
            parts.append(ep)
            print(f"  {stone}_{pos}: {t0} -> {t1} "
                  f"({len(ep)} rec, {campaign_label(t0)})")
        if not parts:
            print(f"  {stone}_{pos}: NO deployed episode found")
            continue
        ep = pd.concat(parts, ignore_index=True)
        ep["stone"] = stone
        ep["position"] = "oben" if pos == "O" else "unten"
        ep["serial"] = serial
        frames.append(ep[["stone", "position", "serial", "campaign",
                          "datetime", "temp_c", "lux"]])
    return pd.concat(frames, ignore_index=True)


def main():
    loc = read_locations()
    print(f"{len(loc)} logger locations read")
    ts = read_series()
    print(f"{len(ts)} deployed-window records from {ts['serial'].nunique()} loggers")

    missing = sorted(set(ts["stone"]) - set(loc.index))
    if missing:
        raise SystemExit(f"stones without coordinates: {missing}")

    ts = ts.join(loc[["x", "y", "z"]], on="stone")
    ts["lux"] = pd.to_numeric(ts["lux"], errors="coerce")
    geom = [Point(xy) for xy in zip(ts["x"], ts["y"])]
    ts = ts.rename(columns={"z": "elevation_m"}).drop(columns=["x", "y"])
    gdf = gpd.GeoDataFrame(ts, geometry=geom, crs=CRS)

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
    print(gdf.groupby('campaign')['temp_c'].describe().round(2).to_string())


if __name__ == "__main__":
    main()
