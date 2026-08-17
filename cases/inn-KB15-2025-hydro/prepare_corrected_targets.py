"""Prepare corrected calibration-target CSVs for the KB15 multiflow calibration.

Applies the July-2026 ground-truth QA findings (see README.md, "Data
particularities"):

1. **DGPS pole-height correction (Sept-25 campaign).** The raw DGPS ``z`` is
   the GNSS antenna elevation; the rover pole was 2.26 m in the upstream pool
   (verticals 1501-1511, cross-validated against the Nov-25 ground survey),
   2.70 m in the downstream pool (1513-1530) and 2.51 m for the transitional
   vertical 1512. Corrected bed elevations live in
   ``dgps-flowtracker-kb15-sept25-zcorrected.gpkg``.
2. **Bathymetry-corrected depth targets.** The photogrammetric 2025 DEM is
   ~0.3 m too high in the wetted channel (no water penetration), so raw
   measured depths are NOT comparable to modelled depths. The comparable
   quantity is the water-surface elevation: the target written to
   ``WATER DEPTH_DATA`` is ``WSE_measured - bed_model`` (the depth the model
   should show at that point given its own bed) - mathematically a
   water-level calibration on the existing WATER DEPTH extraction.
3. **Profile-averaged velocities (Sept-25).** 20 of 30 verticals carry
   3-point profiles (~0.3h/0.6h/0.9h, 'profiles' sheet); their target becomes
   the USGS 3-point depth average ``(u02 + 2*u06 + u08) / 4`` instead of the
   single 0.6h proxy (mean shift -0.015 m/s, up to 0.08 m/s).

Outputs (schema: id, x, y, z, SCALAR VELOCITY_DATA/_ERROR,
WATER DEPTH_DATA/_ERROR, label):

* ``axqua-case/preprocessing/measurements-corrected-q47-3.csv``
* ``axqua-case/preprocessing/measurements-corrected-q48-45.csv``

Run: mamba run -n axqua-env python cases/inn-KB15-2025-hydro/prepare_corrected_targets.py
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib.tri as mtri
import numpy as np
import pandas as pd

from axqua.campaigns import compile_adapter, compile_transect, read_xlsx_sheet
from axqua.selafin import read_slf

HERE = Path(__file__).resolve().parent
GEO = HERE / "user-sources/geodata"
GT = HERE / "user-sources/ground-truth/hydraulics"
OUT = HERE / "axqua-case/preprocessing"

DGPS_CORRECTED = GEO / "flowtracker2/dgps-flowtracker-kb15-sept25-zcorrected.gpkg"
GEOMETRY = HERE / "axqua-case/simulation/geometry.slf"

WSE_ERROR_SEPT = 0.05   # m: RTK z (3 cm) + depth reading (2 cm) + pool flatness
WSE_ERROR_NOV = 0.10    # m: unresolved z/depth inconsistency across the transect
NOV_VEL_ERR_FLOOR = 0.05
# Structural velocity discrepancy from the wetted-channel DEM bias (~0.3 m too
# high): continuity through the reduced section over-predicts point velocity by
# dU ~ U*dh/h ~ 0.1-0.2 m/s. Added in quadrature so velocity informs but cannot
# dominate the joint likelihood the way the unbiased water-level target does.
VEL_DISCREPANCY = {"q47-3": 0.10, "q48-45": 0.20}


def model_bed_interpolator():
    g = read_slf(str(GEOMETRY))
    bottom = np.asarray(g["values"]["BOTTOM"]).ravel()
    tri = mtri.Triangulation(np.asarray(g["x"]), np.asarray(g["y"]),
                             np.asarray(g["ikle"]))  # read_slf returns 0-based
    return mtri.LinearTriInterpolator(tri, bottom)


def profile_depth_averages() -> dict[int, float]:
    """USGS 3-point depth-averaged velocity per multi-depth Sept-25 vertical."""
    raw = read_xlsx_sheet(GT / "FT_TKE_Summary.xlsx", "profiles")
    df = raw.iloc[2:].copy()
    df.columns = raw.iloc[1].tolist()
    df = df[df["Site"] == "KB15"]
    for col in ("ID", "% Depth", "v(x) raw [m/s]", "v(y) raw [m/s]"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["uh"] = np.hypot(df["v(x) raw [m/s]"], df["v(y) raw [m/s]"])
    averages = {}
    for vid, grp in df.groupby("ID"):
        if len(grp) < 3:
            continue
        pct = grp["% Depth"].to_numpy()
        u = grp["uh"].to_numpy()
        u02 = u[np.argmin(np.abs(pct - 0.2))]
        u06 = u[np.argmin(np.abs(pct - 0.6))]
        u08 = u[np.argmin(np.abs(pct - 0.8))]
        averages[int(vid)] = (u02 + 2.0 * u06 + u08) / 4.0
    return averages


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    bed_at = model_bed_interpolator()

    # ---- q47-3 (Sept-25): corrected positions, profile-averaged velocity ----
    base = compile_adapter(GT / "FT_TKE_Summary.xlsx", DGPS_CORRECTED, 25832,
                           label="q47-3")
    dgps = gpd.read_file(DGPS_CORRECTED)
    measured_h = dgps.set_index("ID")["WaterDepth"]
    ids = dgps["ID"].to_numpy()  # row order matches the adapter join order

    u_avg = profile_depth_averages()
    replaced = base["id"].map(
        lambda i: u_avg.get(int(ids[int(i) - 1]), np.nan))
    n_prof = replaced.notna().sum()
    base.loc[replaced.notna(), "SCALAR VELOCITY_DATA"] = replaced.dropna().round(6)

    wse = base["z"].to_numpy() + measured_h.loc[ids].to_numpy()
    bed = bed_at(base["x"].to_numpy(), base["y"].to_numpy()).filled(np.nan)
    base["WATER DEPTH_DATA"] = np.round(wse - bed, 3)
    base["WATER DEPTH_ERROR"] = WSE_ERROR_SEPT
    base["SCALAR VELOCITY_ERROR"] = np.round(np.sqrt(
        base["SCALAR VELOCITY_ERROR"] ** 2 + VEL_DISCREPANCY["q47-3"] ** 2), 4)
    out1 = OUT / "measurements-corrected-q47-3.csv"
    base.to_csv(out1, index=False)
    print(f"q47-3: {len(base)} points ({n_prof} profile-averaged velocities), "
          f"depth targets {base['WATER DEPTH_DATA'].min():.2f}.."
          f"{base['WATER DEPTH_DATA'].max():.2f} m -> {out1.name}")

    # ---- q48-45 (Nov-25): flat-WSE anchored depth targets -------------------
    # The transect's own z + depth values are internally inconsistent (implied
    # WSE spreads 0.34 m across 10 m of one cross-section - physically
    # impossible; flagged for field-data QA). Its WSE is therefore anchored to
    # the pole-corrected Sept-25 pool-1 water surface 10 m away (Q differs by
    # only 2.4%, ~1 cm of stage), which is corroborated by the hardware pole
    # locks; the transect's vertical 1 (z + h = 376.30 m) agrees with it.
    nov = compile_transect(GT / "FT_TKE_Summary_Nov25.xlsx",
                           GEO / "TKE_KB15_Nov25.gpkg", "KB15",
                           NOV_VEL_ERR_FLOOR, label="q48-45")
    pool1 = dgps.iloc[:11]
    wse_transect = float(np.median(pool1["z"].to_numpy()
                                   + pool1["WaterDepth"].to_numpy()))
    bed = bed_at(nov["x"].to_numpy(), nov["y"].to_numpy()).filled(np.nan)
    nov["WATER DEPTH_DATA"] = np.round(wse_transect - bed, 3)
    nov["WATER DEPTH_ERROR"] = WSE_ERROR_NOV
    nov["SCALAR VELOCITY_ERROR"] = np.round(np.sqrt(
        nov["SCALAR VELOCITY_ERROR"] ** 2 + VEL_DISCREPANCY["q48-45"] ** 2), 4)
    out2 = OUT / "measurements-corrected-q48-45.csv"
    nov.to_csv(out2, index=False)
    print(f"q48-45: {len(nov)} points, transect WSE {wse_transect:.3f} m, "
          f"depth targets {nov['WATER DEPTH_DATA'].min():.2f}.."
          f"{nov['WATER DEPTH_DATA'].max():.2f} m -> {out2.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
