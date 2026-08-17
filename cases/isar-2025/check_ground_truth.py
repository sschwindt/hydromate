"""Compare a built scenario against the September-2025 FlowTracker campaign.

Reads the campaign workbook ``FT_TKE_Summary_Sep25.xlsx`` (``Isar`` tab) directly
from its XML - ``pandas.read_excel`` fails on these SonTek exports because
openpyxl's style parser chokes on their ``PatternFill ... extLst`` (see CLAUDE.md)
- and reports three things:

1. **The campaign discharge**, integrated per transect with the mid-section method.
   Each vertical contributes ``D * w * U``; the depth-average ``U`` is taken three
   ways (all points / the 0.6-depth point / the 0.2-0.8 average) so the number can
   be seen not to depend on that choice.
2. **Depth and velocity at every vertical**, against the nearest mesh node of the
   scenario result.
3. **The cross-section ("baffle") discharges** of the run, next to the measured
   transect that sits on the same section.

Run:  python cases/isar-2025/check_ground_truth.py [scenario ...]
      (default: prescribed-q green-ampt)
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from axqua.sections import line_discharges
from axqua.selafin import read_slf

CASE = Path(__file__).resolve().parent
SCENARIOS = CASE / "axqua-case" / "scenarios"
CAMPAIGN = (CASE / "user-sources/ground-truth/flowtracker/Sep25"
            / "FT_TKE_Summary_Sep25.xlsx")
BAFFLES = CASE / "user-sources/geodata/baffles.gpkg"
CRS = 25832

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: measured transect(s) to compare each baffle line against, with how far apart the
#: two actually are (checked against the layer geometry, see the module docstring).
#: `right-main` and `downstream` are co-located with their transects; `left Side`
#: shares the side-channel thread but sits 106 m downstream of the measurement, and
#: the two upstream baffles together correspond to the single combined section
#: `ft_deadwood` 50-125 m below them.
TRANSECT_OF_BAFFLE = {
    "downstream": (["ft_dswood"], "co-located (8 m)"),
    "right-main": (["ft_willows_leavs", "ft_leaves"], "co-located (2-5 m), summed"),
    "left Side": (["ft_sidech_rb"], "same thread, 106 m upstream"),
}
#: baffles whose SUM should match one downstream transect
COMBINED = [(["righ US", "left US"], "ft_deadwood", "combined, 50-125 m downstream")]


def read_campaign(path: Path = CAMPAIGN, sheet: str = "Isar") -> pd.DataFrame:
    """Parse the FlowTracker summary workbook into one row per measured point."""
    z = zipfile.ZipFile(path)
    rels = {r.attrib["Id"]: r.attrib["Target"]
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    book = ET.fromstring(z.read("xl/workbook.xml"))
    target = next(rels[s.attrib[RS + "id"]] for s in book.iter(NS + "sheet")
                  if s.attrib["name"] == sheet)
    shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si")]
    ws = ET.fromstring(z.read("xl/" + target.lstrip("/").replace("xl/", "")))

    grid: dict[int, dict[str, str | None]] = {}
    for row in ws.iter(NS + "row"):
        cells: dict[str, str | None] = {}
        for c in row.iter(NS + "c"):
            col = re.match(r"[A-Z]+", c.attrib["r"]).group()
            inline, value = c.find(NS + "is"), c.find(NS + "v")
            if inline is not None:
                cells[col] = "".join(x.text or "" for x in inline.iter(NS + "t"))
            elif value is None:
                cells[col] = None
            elif c.attrib.get("t") == "s":
                cells[col] = shared[int(value.text)]
            else:
                cells[col] = value.text
        grid[int(row.attrib["r"])] = cells

    header = {col: name for col, name in grid[4].items() if name}
    rows, current = [], None
    for r in sorted(grid):
        if r < 5:
            continue
        cells = grid[r]
        current = cells.get("A") or current          # the file name spans its block
        rows.append({"file": current,
                     **{header[c]: v for c, v in cells.items() if c in header}})
    df = pd.DataFrame(rows)
    for col in ("East.", "North.", "Loc. [m]", "% Depth", "Meas. Depth [m]",
                "Total Depth [m]", "v(x) [m/s]", "v(y) [m/s]", "v(z) [m/s]"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["vertical"] = df["Station"].astype(str).str.split("-").str[0]
    return df


def _vertical_speed(points: pd.DataFrame, method: str) -> float:
    """Depth-averaged speed of one vertical by *method*."""
    points = points.dropna(subset=["v(x) [m/s]"])
    if points.empty:
        return 0.0
    frac = points["% Depth"].to_numpy()
    speed = np.hypot(points["v(x) [m/s]"], points["v(y) [m/s]"]).to_numpy()
    at_06 = np.isclose(frac, 0.6)
    if method == "all":
        return float(speed.mean())
    if method == "0.6":
        return float(speed[at_06].mean()) if at_06.any() else float(speed.mean())
    upper, lower = np.isclose(frac, 0.2), np.isclose(frac, 0.8)
    if upper.any() and lower.any():
        return float((speed[upper].mean() + speed[lower].mean()) / 2)
    return float(speed[at_06].mean()) if at_06.any() else float(speed.mean())


def transect_discharge(df: pd.DataFrame, method: str = "all") -> pd.DataFrame:
    """Mid-section discharge of every measured transect."""
    out = []
    for name, group in df.groupby("file", sort=False):
        verticals = []
        for _, points in group.groupby("vertical", sort=False):
            depth = points["Total Depth [m]"].iloc[0]
            station = points["Loc. [m]"].iloc[0]
            if not np.isfinite(depth) or not np.isfinite(station):
                continue
            verticals.append((station, depth, _vertical_speed(points, method)))
        if len(verticals) < 3:      # fewer than 3 verticals is not a section at all
            continue
        verticals.sort()
        loc = np.array([v[0] for v in verticals])
        depth = np.array([v[1] for v in verticals])
        speed = np.array([v[2] for v in verticals])
        width = np.zeros(len(loc))
        width[0] = abs(loc[1] - loc[0]) / 2
        width[-1] = abs(loc[-1] - loc[-2]) / 2
        width[1:-1] = np.abs(loc[2:] - loc[:-2]) / 2
        out.append({"transect": name, "verticals": len(loc),
                    "width": float(abs(loc[-1] - loc[0])),
                    "area": float((depth * width).sum()),
                    "discharge": float((depth * width * speed).sum()),
                    "east": float(group["East."].mean()),
                    "north": float(group["North."].mean())})
    return pd.DataFrame(out)


def compare(scenario: str, df: pd.DataFrame) -> None:
    sim = SCENARIOS / scenario / "simulation"
    geo, res = sim / "geometry.slf", sim / "r2d.slf"
    if not res.exists():
        print(f"\n=== {scenario}: no result at {res} - run initial_run.py first")
        return
    g, r = read_slf(geo), read_slf(res)
    x, y = np.asarray(g["x"]), np.asarray(g["y"])
    values = r["values"]
    h = np.asarray(values["WATER DEPTH"], float)
    speed = np.hypot(values["VELOCITY U"], values["VELOCITY V"])

    print(f"\n=== {scenario}  (simulated t = {r['time']:.0f} s) ===")
    verticals = (df.dropna(subset=["Total Depth [m]", "East."])
                 .assign(key=lambda d: d["file"] + "|" + d["vertical"])
                 .groupby("key")
                 .agg(file=("file", "first"), east=("East.", "first"),
                      north=("North.", "first"), depth=("Total Depth [m]", "first"),
                      vx=("v(x) [m/s]", "mean"), vy=("v(y) [m/s]", "mean")))
    verticals["u_gt"] = np.hypot(verticals.vx, verticals.vy)
    _, idx = cKDTree(np.column_stack([x, y])).query(
        np.column_stack([verticals.east, verticals.north]))
    verticals["h_mod"] = h[idx]
    verticals["u_mod"] = speed[idx]

    print(f"  {'transect':<20}{'n':>4}{'depth GT':>10}{'model':>8}"
          f"{'|U| GT':>9}{'model':>8}")
    for name, group in verticals.groupby("file"):
        if len(group) < 5:
            continue
        print(f"  {name:<20}{len(group):>4}{group.depth.mean():>10.3f}"
              f"{group.h_mod.mean():>8.3f}{group.u_gt.mean():>9.3f}"
              f"{group.u_mod.mean():>8.3f}")

    sections = line_discharges(res, BAFFLES, geometry=geo,
                               name_field="Baffle Name", crs_epsg=CRS)
    measured = transect_discharge(df).set_index("transect")["discharge"]
    model = sections.set_index("name")["discharge"]
    print(f"\n  {'baffle':<18}{'model Q':>10}{'measured Q':>12}   compared against")
    for name, q in model.items():
        names, note = TRANSECT_OF_BAFFLE.get(name, ([], ""))
        total = sum(float(measured[n]) for n in names if n in measured.index)
        shown = f"{total:>12.3f}" if total else f"{'-':>12}"
        label = f"{'+'.join(names)} - {note}" if names else "(no transect on this line)"
        print(f"  {name:<18}{q:>10.3f}{shown}   {label}")
    for parts, transect, note in COMBINED:
        if all(p in model.index for p in parts) and transect in measured.index:
            print(f"  {'+'.join(parts):<18}{model[list(parts)].sum():>10.3f}"
                  f"{measured[transect]:>12.3f}   {transect} - {note}")


def main() -> None:
    df = read_campaign()
    print(f"September-2025 FlowTracker campaign: {CAMPAIGN.name}")
    print("\nMeasured discharge per transect (mid-section method):")
    print(f"  {'transect':<20}{'n':>4}{'width':>8}{'area':>8}"
          f"{'Q(all)':>9}{'Q(0.6)':>9}{'Q(.2/.8)':>10}")
    base = transect_discharge(df, "all").set_index("transect")
    q06 = transect_discharge(df, "0.6").set_index("transect")["discharge"]
    q28 = transect_discharge(df, "0.2/0.8").set_index("transect")["discharge"]
    for name, row in base.iterrows():
        print(f"  {name:<20}{row.verticals:>4.0f}{row.width:>8.1f}{row.area:>8.2f}"
              f"{row.discharge:>9.3f}{q06[name]:>9.3f}{q28[name]:>10.3f}")
    full = base[base.width > 10]
    if len(full) >= 2:
        print(f"\n  The full cross-sections ({', '.join(full.index)}) agree to "
              f"{100 * full.discharge.std() / full.discharge.mean():.1f}%, "
              f"mean {full.discharge.mean():.2f} m3/s - the reach discharge during "
              "the campaign.")

    for scenario in (sys.argv[1:] or ["prescribed-q", "green-ampt"]):
        compare(scenario, df)


if __name__ == "__main__":
    main()
