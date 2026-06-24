# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A TELEMAC hydro-morphodynamic model of a reach of the **Inn river in Bavaria, Germany**.
The goal is a 2D depth-averaged hydraulic + sediment-transport simulation (TELEMAC-2D
coupled with GAIA), set up automatically and calibrated with quantified uncertainty via
[HydroBayesCal](https://github.com/Ecohydraulics/hydrobayescal).

The repository contains both **input data** (`geodata/`, `stage-discharge/`) and the
**`tmsetup` package** (`src/tmsetup/`) that turns that data into a calibration-ready
TELEMAC case. See `README.md` for the workflow and `config/inn.yml` for the case config.

## The `tmsetup` workflow

`tm-setup config/inn.yml` runs a 5-stage pipeline (`src/tmsetup/pipeline.py`):

1. `dem.py` — reproject + clip the initial (and optional target) DEM to the ROI boundary.
2. `mesh.py` + `selafin.py` — gmsh triangular mesh from boundary + breaklines with
   per-MATID size fields; DEM interpolated onto nodes; geometry written as SELAFIN with a
   `FRIC_ID` per-node variable (so no separate ZONES FILE is needed). The mesh boundary is
   walked to build IPOBO, which **must** stay consistent with the `.cli` row order.
3. `boundary.py` — classify contour nodes against the liquid-boundary lines and write the
   `.cli`. Codes (from the working Inn model): wall `2 2 2`, inflow `5 5 5`, outflow `5 4 4`.
4. `steering.py` — friction `.tbl` (one row per MATID: `<id> <LAW> <coef> NULL`, ends with
   `END`; comments only on `*` lines) and the Telemac2d `.cas`; GAIA `.cas` when enabled.
5. `calibration.py` — `measurements-calibration.csv` and the HydroBayesCal
   `config_Telemac.py`.

Key design points:
- The build runs in its **own** `telemac-inn` conda env (gmsh/geopandas/rasterio); it does
  not import TELEMAC's Python. `env.py` *sources* `telemac.pysource` (the `pysource.*.sh`)
  in a subshell for any solver/SELAFIN call. The config's `telemac.pysource` must point at
  the real script (e.g. `/home/schwindt/opt/telemac/configs/pysource.mint22.sh`).
- Friction zones come from the **MATID** scheme in `geodata/shapefiles/region-pts-table.txt`
  (1 riverbed_fine … 5 floodplain) → friction `.tbl` rows and `zone<MATID>` calibration
  parameters. HydroBayesCal perturbs the coefficient column of the `.tbl`.
- `src/tmsetup/selafin.py` is a self-contained big-endian SELAFIN writer; its output is
  validated against TELEMAC's own `data_manip.formats.selafin.Selafin` reader in the tests.
- Calibration CSV schema (read by HydroBayesCal): `id, x, y, z, <QTY>_DATA, <QTY>_ERROR`,
  where `<QTY>` is a SELAFIN name (e.g. `WATER DEPTH`, `SCALAR VELOCITY`).

v1 covers the **2D hydraulic** path end to end; GAIA morphodynamics and the
DEM-of-Difference topographic-change calibration are wired as extension points
(`morphodynamics` config block, `dem.dem_of_difference`, `steering.write_gaia_cas`).

### Build, test, run

```bash
mamba env create -f environment.yml && mamba activate telemac-inn && pip install -e .
tm-setup config/inn.yml --check        # validate config only
tm-setup config/inn.yml                # build the case
tm-setup config/inn.yml --dry-run      # build, then run the solver once to validate
mamba run -n telemac-inn pytest tests/ # end-to-end test on synthetic fixtures (no solver)
```

The user must supply a **ROI boundary polygon** (`inputs.boundary`, a closed polygon/polyline
in EPSG:25832) — it is not in `geodata/` yet.

## Environment

TELEMAC is installed at `/home/schwindt/opt/telemac`. Activate it before running any
TELEMAC tool (`telemac2d.py`, `gaia`, `stbtel`, mesh utilities, the `telapy` Python API):

```bash
source /home/schwindt/opt/telemac/configs/pysource.mint22.sh
```

This sets `HOMETEL`, selects config `systel.mint22.cfg`, and uses the `ubugfopenmpi`
compiler/MPI setup. Module documentation lives in
`/home/schwindt/opt/telemac/documentation/{telemac2d,gaia,stbtel,gretel,...}` and worked
examples in `/home/schwindt/opt/telemac/examples`.

For geodata scripting (GDAL/rasterio/geopandas/pyproj), use the `wrr-proj` mamba env:

```bash
mamba run -n wrr-proj python <script>
```

Note: GDAL is **not** in `wrr-proj` by default in all cases — if `from osgeo import gdal`
fails, prefer the TELEMAC pysource environment (which ships GDAL) or `rasterio`.

## Coordinate system — non-negotiable

**Everything is EPSG:25832 (ETRS89 / UTM Zone 32N), units in metres.** This was verified
for the DEMs, all shapefiles, and the gauge metadata. Any new raster, mesh, or shapefile
must use the same CRS, or downstream alignment (DEM interpolation onto the mesh, breakline
snapping) will silently break.

Vertical datum varies across the gauge data (DHHN12 / DHHN2016 / "m NN" vs "m NHN") — check
and reconcile datum before using gauge zero-point heights as model boundary elevations.

## Data layout and meaning

### `geodata/`
- `dem-2020.tif` — 0.5 m DEM (~35748×20240), the baseline terrain.
- `dem-2025-0.20m-res.tif` — 0.20 m high-res DEM (~444 MB), newer survey.
- `ortho-2020.tif` — orthophoto, 2020.
- `iws-uas/Inn_KB8_*_Ortho.tif` — UAS/drone orthophoto (2025 survey of subreach KB8).
- `qgismesh.slf` — a **Selafin** mesh (QGIS/BlueKenue export). This is the working
  computational mesh; the Inn geometry/mesh used for TELEMAC derives from it.
- `visInn.qgz` — QGIS project tying the layers together for visualization.
- `shapefiles/` — mesh-generation inputs:
  - `liquid-boundaries.*` — inflow/outflow boundary segments (drives the TELEMAC `.cli`).
  - `breaklines.*` — hard breaklines (channel edges, structures) to constrain the mesh.
  - `region-points.*` / `region-points-grob.gpkg` — region seed points carrying mesh
    refinement + material classification (see table below).
  - `region-pts-table.txt` — the authoritative mapping of region seed points to mesh
    resolution and material IDs.

### Material / region classification (`region-pts-table.txt`)

Region points assign a max triangle area (mesh density) and a `MATID` (material zone) used
later for roughness and sediment classes in GAIA. Base resolution is ~0.2 m.

| MATID | name           | max area (m²) | type        | meaning                              |
|-------|----------------|---------------|-------------|--------------------------------------|
| 1     | riverbed_fine  | 1.0           | riverbed    | fine bed (sand, silt)                |
| 2     | riverbed_coarse| 4.0           | riverbed    | coarse bed (gravel, cobbles)         |
| 3     | block_ramp     | 0.5           | block_ramp  | block ramp / high-roughness structure|
| 4     | gravel_bank    | 2.0           | gravel_bank | gravel bank / transition zone        |
| 5     | floodplain     | 25.0          | floodplain  | floodplain, vegetated overbank       |

### `stage-discharge/` — boundary-condition source data (Nov 2020, 15-min interval)

Gauge exports from the Bavarian LfU (gkd.bayern.de). All cover 2020-11-01 → 2020-11-30,
2890 data rows each.

- `fluesse-abfluss/18004007_*.csv` — **discharge** [m³/s], gauge **Kraiburg** (Q ≈ 47 m³/s
  mean for the period; min 46.2 / max 49.5 per the notes in `region-pts-table.txt`).
- `fluesse-wasserstand/18003004_*.csv` — **stage** [cm], gauge **Wasserburg** (upstream).
- `fluesse-wasserstand/18004506_*.csv` — **stage** [cm], gauge **Mühldorf** (downstream).

These feed the model's upstream (discharge) and downstream (water level) liquid boundaries.

**CSV parsing gotchas** (German LfU format):
- UTF-8 **BOM** on the first line.
- **Semicolon** delimiter, **comma** decimal separator (e.g. `43,3`).
- 9 metadata lines (Quelle, station name/no., Gewässer, gauge coordinates as
  `Ostwert`/`Nordwert`, `Pegelnullpunktshöhe`), a blank line, then a header row
  (`Datum;"...[unit]";Prüfstatus`), then `"YYYY-MM-DD HH:MM";value;Geprueft` rows.
- Times are MEZ (CET). Stage is in **cm**; convert to m and add the gauge zero-point height
  for absolute water-surface elevation.

Read with `pandas`: `pd.read_csv(path, sep=';', decimal=',', skiprows=10, encoding='utf-8-sig')`.

## Working conventions

- Large rasters (the DEMs/orthos run to hundreds of MB) — don't open them blindly; use
  `gdalinfo`/`rasterio` to read headers, and window/downsample for any processing.
- Keep raw input data immutable. Write generated meshes, `.cas`/`.cli` files, and results
  into clearly separated locations rather than mixing them into `geodata/`.
- This directory is **not** a git repository.
