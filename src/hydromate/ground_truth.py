"""Ground-truth ingestion: field measurements -> tidy calibration tables.

Calibration ground truth comes in two halves that live in separate places and
are joined here:

* **positions** — a point layer (shapefile/GeoPackage) giving where each
  measurement was taken, in *some* CRS (often not the project CRS); reprojected
  to the project CRS on ingest.
* **values** — the measured quantities, in a source-specific export (e.g. a
  SonTek FlowTracker2 ``.ft.sum`` workbook).

Every source is normalised to the same **tidy** schema: the first three columns
are ``x, y, z`` (project CRS, metres), followed by quantity columns. For
FlowTracker hydraulics these are ``u, v, w`` (velocity components, m/s),
``u_err, v_err, w_err`` (per-component error, m/s) and ``h`` (water depth, m).

FlowTracker is only *one* possible source; the tidy schema is generic so other
ground-truth (e.g. ADCP, hand-held probes, sediment samples) can be added as
further adapters without touching the calibration stage that consumes the tidy
tables.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# --------------------------------------------------------------------------- #
# Robust .xlsx reader (bypasses openpyxl, which chokes on the FlowTracker and
# GSD workbooks: "PatternFill.__init__() got an unexpected keyword 'extLst'").
# We read the worksheet XML and shared strings straight out of the zip.
# --------------------------------------------------------------------------- #
def _col_index(ref: str) -> int:
    """Spreadsheet cell ref (e.g. ``AB12``) -> 0-based column index."""
    letters = re.match(r"([A-Z]+)", ref).group(1)
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def read_xlsx_sheet(path: Path, sheet: int | str = 0) -> pd.DataFrame:
    """Read one worksheet into a raw, header-less :class:`~pandas.DataFrame`.

    Cells keep their string form except plain numbers, which are coerced to
    float. Robust to the styling that defeats ``pandas.read_excel``/openpyxl.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(_NS + "t"))
                      for si in sst.iter(_NS + "si")]

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.iter(_NS + "sheet")]
        if isinstance(sheet, str):
            sheet_no = names.index(sheet) + 1
        else:
            sheet_no = sheet + 1
        sheet_xml = f"xl/worksheets/sheet{sheet_no}.xml"
        if sheet_xml not in z.namelist():
            members = sorted(p for p in z.namelist()
                             if re.match(r"xl/worksheets/sheet\d+\.xml", p))
            sheet_xml = members[sheet if isinstance(sheet, int) else 0]
        ws = ET.fromstring(z.read(sheet_xml))

    rows: list[dict[int, object]] = []
    for r in ws.iter(_NS + "row"):
        cells: dict[int, object] = {}
        for c in r.findall(_NS + "c"):
            v = c.find(_NS + "v")
            if v is None or v.text is None:
                continue
            if c.get("t") == "s":
                val: object = shared[int(v.text)]
            else:
                try:
                    val = float(v.text)
                except ValueError:
                    val = v.text
            cells[_col_index(c.get("r"))] = val
        rows.append(cells)

    if not rows:
        return pd.DataFrame()
    ncol = max((max(c) for c in rows if c), default=-1) + 1
    data = [[row.get(i) for i in range(ncol)] for row in rows]
    return pd.DataFrame(data)


# --------------------------------------------------------------------------- #
# FlowTracker2 adapter
# --------------------------------------------------------------------------- #
# header label in the .ft.sum sheet  ->  tidy column name
_FT_VALUE_COLUMNS = {
    "VelX": "u", "VelY": "v", "VelZ": "w",
    "VxErr": "u_err", "VyErr": "v_err", "VzErr": "w_err",
    "FinalD": "h",
}


def read_flowtracker_values(xlsx: Path) -> pd.DataFrame:
    """Read a SonTek FlowTracker2 ``.ft.sum`` workbook -> per-vertical values.

    Returns columns ``ID, u, v, w, u_err, v_err, w_err, h`` (one row per
    measurement vertical). Coordinates are *not* here — they come from the
    paired DGPS position layer (see :func:`read_flowtracker`).
    """
    raw = read_xlsx_sheet(Path(xlsx))
    # the header row is the one whose first cell is "ID"; units row follows it.
    header_idx = next(i for i in range(len(raw))
                      if str(raw.iloc[i, 0]).strip() == "ID")
    header = [str(h).strip() if h is not None else "" for h in raw.iloc[header_idx]]
    data = raw.iloc[header_idx + 2:].copy()       # skip header + units rows
    data.columns = header
    data = data[data["ID"].notna()]

    out = pd.DataFrame()
    out["ID"] = pd.to_numeric(data["ID"], errors="coerce").astype("Int64")
    for src, dst in _FT_VALUE_COLUMNS.items():
        if src in data.columns:
            out[dst] = pd.to_numeric(data[src], errors="coerce")
    return out.dropna(subset=["ID"]).reset_index(drop=True)


def read_flowtracker(xlsx: Path, positions: Path, crs_epsg: int,
                     join_key: str = "ID") -> pd.DataFrame:
    """Join FlowTracker values to their DGPS positions -> tidy hydraulics table.

    Parameters
    ----------
    xlsx : the ``.ft.sum`` export with the measured velocities/depths.
    positions : point layer (shp/gpkg) of the survey points, with a matching
        ``join_key`` column; reprojected to ``crs_epsg`` for the x/y output.
    crs_epsg : project CRS the output coordinates are expressed in.

    Returns the tidy schema ``x, y, z`` then ``u, v, w, u_err, v_err, w_err, h``.
    """
    import geopandas as gpd

    values = read_flowtracker_values(Path(xlsx))

    pts = gpd.read_file(Path(positions))
    if pts.crs is not None and pts.crs.to_epsg() != crs_epsg:
        pts = pts.to_crs(epsg=crs_epsg)
    pcols = {c.lower(): c for c in pts.columns}
    key = pcols.get(join_key.lower())
    if key is None:
        raise ValueError(
            f"position layer {Path(positions).name!r} has no '{join_key}' column "
            f"to join on (has {[c for c in pts.columns if c != 'geometry']})"
        )
    pos = pd.DataFrame({
        "ID": pd.to_numeric(pts[key], errors="coerce").astype("Int64"),
        "x": pts.geometry.x.to_numpy(),
        "y": pts.geometry.y.to_numpy(),
    })
    zcol = pcols.get("z")
    pos["z"] = pd.to_numeric(pts[zcol], errors="coerce") if zcol else 0.0

    merged = pos.merge(values, on="ID", how="inner", validate="one_to_one")
    if len(merged) < len(values):
        missing = sorted(set(values["ID"].dropna()) - set(pos["ID"].dropna()))
        raise ValueError(
            f"{len(values) - len(merged)} FlowTracker vertical(s) had no matching "
            f"position in {Path(positions).name!r} (unmatched IDs: {missing})"
        )

    lead = ["x", "y", "z"]
    rest = [c for c in merged.columns if c not in (*lead, "ID")]
    return merged[[*lead, *rest]].reset_index(drop=True)


def scalar_velocity(df: pd.DataFrame) -> pd.Series:
    """Depth-averaged scalar velocity magnitude from the components present."""
    comps = [df[c] for c in ("u", "v", "w") if c in df.columns]
    return np.sqrt(sum(c.astype(float) ** 2 for c in comps))


# --------------------------------------------------------------------------- #
# Canonical tidy table: one sheet per category, columns ``x, y, z`` then
# quantities. Column headers are normalised to canonical names where known;
# unrecognised columns pass through unchanged so arbitrary quantities work.
# --------------------------------------------------------------------------- #
COORD_COLUMNS = ("x", "y", "z")

_COLUMN_ALIASES = {
    "x": "x", "easting": "x", "ostwert": "x", "e": "x",
    "y": "y", "northing": "y", "nordwert": "y", "n": "y",
    "z": "z", "elevation": "z", "bed": "z", "bed_level": "z",
    "u": "u", "vx": "u", "velx": "u", "u_x": "u", "ux": "u",
    "v": "v", "vy": "v", "vely": "v", "u_y": "v", "uy": "v",
    "w": "w", "vz": "w", "velz": "w", "u_z": "w", "uz": "w",
    "u_err": "u_err", "u'": "u_err", "uerr": "u_err", "vxerr": "u_err",
    "v_err": "v_err", "v'": "v_err", "verr": "v_err", "vyerr": "v_err",
    "w_err": "w_err", "w'": "w_err", "werr": "w_err", "vzerr": "w_err",
    "h": "h", "depth": "h", "water_depth": "h", "waterdepth": "h",
    "measd": "h", "finald": "h",
}


def _canonical_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: _COLUMN_ALIASES.get(str(c).strip().lower(),
                                                    str(c).strip().lower())
                            for c in df.columns})
    lead = [c for c in COORD_COLUMNS if c in df.columns]
    rest = [c for c in df.columns if c not in COORD_COLUMNS]
    return df[[*lead, *rest]]


def read_points(path: Path, crs_epsg: int) -> pd.DataFrame:
    """Read a point layer/CSV that already carries coords + quantities -> tidy.

    A spatial layer (shp/gpkg/geojson) is reprojected to ``crs_epsg`` and its
    geometry becomes ``x, y``; a CSV must provide ``x``/``y`` columns (or
    aliases). All other columns pass through (canonicalised where recognised).
    """
    path = Path(path)
    if path.suffix.lower() in (".shp", ".gpkg", ".geojson"):
        import geopandas as gpd

        gdf = gpd.read_file(path)
        if gdf.crs is not None and gdf.crs.to_epsg() != crs_epsg:
            gdf = gdf.to_crs(epsg=crs_epsg)
        df = pd.DataFrame(gdf.drop(columns=gdf.geometry.name))
        df = _canonical_columns(df)
        df["x"] = gdf.geometry.x.to_numpy()       # geometry wins over any x/y attrs
        df["y"] = gdf.geometry.y.to_numpy()
    else:
        df = _canonical_columns(read_xlsx_sheet(path) if path.suffix.lower() in
                                (".xlsx", ".xlsm") else pd.read_csv(path))
    if "z" not in df.columns:
        df["z"] = 0.0
    lead = [c for c in COORD_COLUMNS if c in df.columns]
    return df[[*lead, *[c for c in df.columns if c not in COORD_COLUMNS]]]


def _compile_source(src, crs_epsg: int) -> pd.DataFrame:
    """Run one :class:`~hydromate.config.GroundTruthSource` -> tidy DataFrame."""
    if src.kind == "flowtracker":
        return read_flowtracker(src.values, src.positions, crs_epsg, src.join_key)
    if src.kind == "points":
        layer = src.positions or src.values
        return read_points(layer, crs_epsg)
    raise ValueError(f"unknown ground_truth source kind: {src.kind!r}")


def compile_ground_truth(cfg) -> Path | None:
    """Compile the configured raw sources into the tidy multi-tab table.

    Sources sharing a ``category`` are concatenated into one sheet. Returns the
    written path (``cfg.ground_truth_path``), or ``None`` when no sources are
    configured (the user supplies the tidy table directly).
    """
    if not cfg.inputs.ground_truth:
        return None
    tables: dict[str, list[pd.DataFrame]] = {}
    for src in cfg.inputs.ground_truth:
        tables.setdefault(src.category, []).append(_compile_source(src, cfg.crs_epsg))
    merged = {cat: pd.concat(parts, ignore_index=True) for cat, parts in tables.items()}
    out = cfg.ground_truth_path
    write_tidy(merged, out)
    return out


def read_tidy(path: Path) -> dict[str, pd.DataFrame]:
    """Read the tidy multi-tab ground-truth table -> ``{category: DataFrame}``.

    Each sheet/file becomes one category; columns are canonicalised and any
    quantity columns absent from a given dataset are simply not present
    (callers must tolerate missing quantities).
    """
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        sheets = pd.read_excel(path, sheet_name=None)
        return {name: _canonical_columns(df) for name, df in sheets.items()}
    # a single-table CSV -> one category named after the file stem
    return {path.stem: _canonical_columns(pd.read_csv(path))}


def write_tidy(tables: dict[str, pd.DataFrame], path: Path) -> Path:
    """Write ``{category: DataFrame}`` to a multi-tab xlsx (one sheet per category)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for category, df in tables.items():
            _canonical_columns(df).to_excel(writer, sheet_name=category[:31], index=False)
    return path
