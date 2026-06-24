# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A TELEMAC hydro-morphodynamic model of a reach of the **Inn river in Bavaria, Germany**. The goal is a 2D depth-averaged hydraulic + sediment-transport simulation (TELEMAC-2D coupled with GAIA), set up automatically and calibrated with quantified uncertainty via [HydroBayesCal](https://github.com/Ecohydraulics/hydrobayescal).

**Overarching aim:** largely automate *both* sides of the workflow around HydroBayesCal — **pre-processing** (geodata → ready TELEMAC case) and **post-processing** (field **ground-truth** → calibration targets + result extraction). Pre-processing (stages 1–5) is built for the 2D hydraulic path; the **post-processing / ground-truth ingestion is the active frontier** (turning FlowTracker velocity/depth, grain-size distributions, and DEM-of-Difference topographic change into the calibration-points CSV and GAIA inputs HydroBayesCal consumes). See "Ground-truth & calibration data" below.

The tracked repository is the **`hydromate` package** (`src/hydromate/`) plus its template config (`config/inn.yml`) and worked example (`example-Inn/`). The real Inn **input data** (DEMs, orthos, zone GeoPackages, gauge data) is large and lives under `example-Inn/` for co-development only — it is **gitignored** (`.gitignore` ignores everything under `example-Inn/` except `*.py`/`*.md`, plus `*.tif/*.gpkg/case/*.slf`). Don't assume those files are present in a fresh checkout. See `README.md` for the workflow and `config/inn.yml` for the case config (a commented template; all paths resolve relative to the config file's directory).

## The `hydromate` workflow

`hydromate config/inn.yml` runs a 5-stage pipeline (`src/hydromate/pipeline.py`):

1. `dem.py` — reproject + clip the initial (and optional target) DEM to the ROI boundary.
2. `mesh.py` + `selafin.py` — gmsh triangular mesh; DEM interpolated onto nodes; geometry written as SELAFIN with a `FRIC_ID` per-node variable (so no separate ZONES FILE is needed). The mesh boundary is walked to build IPOBO, which **must** stay consistent with the `.cli` row order. **Two meshing strategies** (auto-selected): *anisotropic* (when `inputs.mesh_zones` + `inputs.channel_centerline` are set) elongates channel triangles along the centerline and keeps floodplain near-equilateral, via a metric-tensor background view + gmsh **BAMG** (`Mesh.Algorithm=7`); *isotropic fallback* uses `default_size`/`breakline_size`/`region_sizes` distance fields. See "Meshing" below.
3. `boundary.py` — classify contour nodes against the liquid-boundary lines and write the `.cli`. Codes (from the working Inn model): wall `2 2 2`, inflow `5 5 5`, outflow `5 4 4`.
4. `steering.py` — friction `.tbl` (one row per MATID: `<id> <LAW> <coef> NULL`, ends with `END`; comments only on `*` lines) and the Telemac2d `.cas`; GAIA `.cas` when enabled. The prescribed inflow Q and outflow water-surface elevation come from `hydraulics.py` (reads `inputs.inflow`; resolves the downstream WSE from `hydrodynamics.prescribed_elevation` or a `inputs.stage_discharge` rating curve).
5. `ground_truth.py` + `calibration.py` — compile the configured `inputs.ground_truth` sources into the **tidy multi-tab table** (`ground_truth.compile_ground_truth`, written to `work_dir/ground-truth.xlsx` unless `inputs.measurements` overrides), then turn it into `measurements-calibration.csv` and the HydroBayesCal `config_Telemac.py`.

`pipeline.run()` returns an `Artifacts` dataclass listing every produced path; `cli.py` logs these. The package's public API (`hydromate/__init__.py`) exports `Config`, `load_config`, `clip_to_roi`, `clip_dem_to_roi`, `compile_ground_truth`, `read_tidy`, and the mesh helpers `build_mesh` / `interpolate_elevations` / `write_mesh`. `mesh.build_mesh(cfg)` (no DEM) yields a bare mesh; `interpolate_elevations(mesh, dem, decimals=4)` fills the bottom from a raster (rounded); `write_mesh(mesh, path)` writes a geometry SELAFIN. The `example-Inn/preprocessing.py` script uses these to emit `mesh-raw.slf` then `mesh-elevations.slf` (both in `work_dir`).

### Meshing (stage 2)

The mesher is **gmsh**, chosen over the SALOME/Q4TS route (which the working Inn model historically used) because gmsh is pip-installable, fully headless/scriptable, and supports the anisotropic meshing needed here — whereas Q4TS runs inside SALOME's GUI/SMESH stack and is heavyweight/version-sensitive to automate. SALOME lives at `/home/schwindt/opt/salome/` if ever needed.

- **Anisotropic, flow-aligned channel** (the requirement): channel triangles are *elongated along the channel centerline*, floodplain triangles near-equilateral, with a smooth transition. Implemented exactly like gmsh tutorial **t17**: build a coarse background triangulation (a Delaunay grid over the ROI bbox, node-budget-capped ~40k so any domain fits in memory), assign each background node a **3×3 metric tensor** (eigenvalue = 1/h²; anisotropic `1/(channel_size·anisotropy)²` along the local centerline tangent and `1/channel_size²` across, inside `channel` zones; isotropic `1/floodplain_size²` elsewhere), push it as a `TT` list **PostView** + `setAsBackgroundMesh`, then mesh with **`Mesh.Algorithm=7` (BAMG)**, `Mesh.SmoothRatio=growth_ratio`, `Mesh.AnisoMax`. The coarse background also produces the smooth channel→floodplain blend (gmsh interpolates the metric within each background triangle).
- Zone selection is by **substring** of the `Zone Name` attribute (case-insensitive): `*channel*` vs `*floodplain*` in `inputs.mesh_zones`. The centerline (`inputs.channel_centerline`) is sampled and `np.gradient` gives per-vertex tangents (nearest via `cKDTree`).
- BAMG **approximates** the metric: realised cross-channel size and anisotropy come out a bit gentler than the configured targets, and `growth_ratio` (default 1.2) intentionally relaxes them near banks. Knobs: `mesh.channel_size` 0.5, `floodplain_size` 1.5, `growth_ratio` 1.2, `channel_anisotropy` 4.0. Defaults are **high-res**: the Inn KB15 ROI (~0.32 km², ~130 m-wide channel) yields ~525k elements / ~264k nodes (~50 s, ~0.6 GB) — raise the sizes while iterating.
- The **isotropic fallback** (no mesh_zones/centerline) is the original distance-field path (`default_size`/`breakline_size`/`region_sizes`); the integration test exercises it.
- Meshing (mesh-zones) and friction (roughness-zones / MATID) are **separate concerns**: `inputs.mesh_zones` only sizes the mesh. Friction still comes from the MATID scheme; wiring `roughness-zones.gpkg` (also `Zone Name` channel/floodplain) into friction is a future step.

Key design points:
- The build runs in its **own** `hydromate-env` conda env (gmsh/geopandas/rasterio); it does not import TELEMAC's Python. `env.py` *sources* `telemac.pysource` (the `pysource.*.sh`) in a subshell for any solver/SELAFIN call. The config's `telemac.pysource` must point at the real script (e.g. `/home/schwindt/opt/telemac/configs/pysource.mint22.sh`).
- Friction zones come from the **MATID** scheme (1 riverbed_fine … 5 floodplain; see table below) carried by `inputs.region_points` + `inputs.region_table` and/or `friction.zones` in the config → friction `.tbl` rows and `zone<MATID>` calibration parameters. HydroBayesCal perturbs the coefficient column of the `.tbl`.
- `src/hydromate/selafin.py` is a self-contained big-endian SELAFIN writer; its output is validated against TELEMAC's own `data_manip.formats.selafin.Selafin` reader in the tests.
- Calibration CSV schema (read by HydroBayesCal): `id, x, y, z, <QTY>_DATA, <QTY>_ERROR`, where `<QTY>` is a SELAFIN name (e.g. `WATER DEPTH`, `SCALAR VELOCITY`).

v1 covers the **2D hydraulic** path end to end; GAIA morphodynamics and the DEM-of-Difference topographic-change calibration are wired as extension points (`morphodynamics` config block, `dem.dem_of_difference`, `steering.write_gaia_cas`).

### Build, test, run

```bash
mamba env create -f environment.yml && mamba activate hydromate-env && pip install -e ".[dev,gui]"
hydromate config/inn.yml --check        # validate config only
hydromate config/inn.yml                # build the case
hydromate config/inn.yml --dry-run      # build, then run the solver once to validate
hydromate config/inn.yml --no-validate-env  # build without sourcing the TELEMAC env
hydromate clip <raster> -b <boundary> -o <out>   # crop one raster to a ROI, no config
mamba run -n hydromate-env pytest tests/ # end-to-end test on synthetic fixtures (no solver)
mamba run -n hydromate-env pytest tests/test_integration.py::<name>  # single test
mamba run -n hydromate-env ruff check src/   # lint (line-length 100)
```

Packaging is `pyproject.toml` (setuptools, `src/` layout); extras: `[dev]` = pytest+ruff, `[gui]` = streamlit. Entry points: `hydromate` (CLI) and `hydromate-gui` (Streamlit config editor — `gui/launch.py` shells out to `streamlit run gui/app.py`).

`example-Inn/` holds the two-step worked example for the Inn case, run against `config/inn.yml`: `preprocessing.py` (clip DEMs to the ROI) then `run2postprocessing.py` (build the case, optional `dry_run`, hand off to HydroBayesCal). These are flat scripts, run with `mamba run -n hydromate-env python example-Inn/<script>.py`.

A build needs a **ROI boundary polygon** (`inputs.boundary`, a closed polygon/polyline in EPSG:25832; the Inn example uses `example-Inn/geodata/roi-kb15.gpkg`) plus `inputs.liquid_boundaries` and `inputs.inflow` — the latter two are still placeholders/TODO in `config/inn.yml` and must be prepared before a full build will run.

## Environment

TELEMAC is installed at `/home/schwindt/opt/telemac`. Activate it before running any TELEMAC tool (`telemac2d.py`, `gaia`, `stbtel`, mesh utilities, the `telapy` Python API):

```bash
source /home/schwindt/opt/telemac/configs/pysource.mint22.sh
```

This sets `HOMETEL`, selects config `systel.mint22.cfg`, and uses the `ubugfopenmpi` compiler/MPI setup. Module documentation lives in `/home/schwindt/opt/telemac/documentation/{telemac2d,gaia,stbtel,gretel,...}` and worked examples in `/home/schwindt/opt/telemac/examples`.

For geodata scripting (GDAL/rasterio/geopandas/pyproj), use the `wrr-proj` mamba env:

```bash
mamba run -n wrr-proj python <script>
```

Note: GDAL is **not** in `wrr-proj` by default in all cases — if `from osgeo import gdal` fails, prefer the TELEMAC pysource environment (which ships GDAL) or `rasterio`.

## Coordinate system — non-negotiable

**Everything is EPSG:25832 (ETRS89 / UTM Zone 32N), units in metres.** This was verified for the DEMs, all shapefiles, and the gauge metadata. Any new raster, mesh, or shapefile must use the same CRS, or downstream alignment (DEM interpolation onto the mesh, breakline snapping) will silently break.

Vertical datum varies across the gauge data (DHHN12 / DHHN2016 / "m NN" vs "m NHN") — check and reconcile datum before using gauge zero-point heights as model boundary elevations.

## Data layout and meaning

The Inn co-development dataset lives under `example-Inn/geodata/` (gitignored). The exact files evolve; what `config/inn.yml` currently points at:
- `dem-2020.tif` — baseline terrain (0.5 m DEM), `inputs.dem_initial`.
- `DEM-2025-20cm.tif` — 0.20 m high-res survey, `inputs.dem_target` (enables the morphodynamic/DoD path).
- `roi-kb15.gpkg` — the ROI / max-wetted-extent polygon (`inputs.boundary`), subreach KB15.
- `liquid-boundaries.gpkg` — inflow/outflow boundary segments (drives the `.cli`).
- `mesh-zones.gpkg` — polygons with a `Zone Name` (`channel`/`floodplain`) driving the anisotropic mesh (`inputs.mesh_zones`). `channel-centerline.gpkg` — the line channel cells are elongated along (`inputs.channel_centerline`).
- `roughness-zones.gpkg` — `Zone Name` polygons (channel/floodplain; note inconsistent casing like `Floodplain`) for friction assignment, *not yet wired* (friction currently uses the MATID scheme).
- `iws-uas/`, `flowtracker2/`, `sediment/`, `muehldorf-gauge-XS/` — ortho/UAS imagery, FlowTracker velocity ground truth, GSDs, and gauge cross-section. `example-Inn/ground-truth/` holds the calibration ground-truth spreadsheets.

`-roi-clip.tif` files are generated clip outputs (from `preprocessing.py` / `hydromate clip`), not raw inputs.

### Material / region classification (MATID scheme)

Region/zone polygons assign a max triangle area (mesh density) and a `MATID` (material zone) used later for roughness and sediment classes in GAIA. Base resolution is ~0.2 m.

| MATID | name           | max area (m²) | type        | meaning                              |
|-------|----------------|---------------|-------------|--------------------------------------|
| 1     | riverbed_fine  | 1.0           | riverbed    | fine bed (sand, silt)                |
| 2     | riverbed_coarse| 4.0           | riverbed    | coarse bed (gravel, cobbles)         |
| 3     | block_ramp     | 0.5           | block_ramp  | block ramp / high-roughness structure|
| 4     | gravel_bank    | 2.0           | gravel_bank | gravel bank / transition zone        |
| 5     | floodplain     | 25.0          | floodplain  | floodplain, vegetated overbank       |

### Boundary-condition gauge data (Bavarian LfU)

The upstream (discharge) and downstream (water level) boundaries are driven by gauge exports from the Bavarian LfU (gkd.bayern.de) — discharge [m³/s] at Kraiburg (Q ≈ 47 m³/s for the Nov-2020 reference period), stage [cm] at Wasserburg (upstream) and Mühldorf (downstream). When such CSVs are supplied via `inputs.inflow` / `inputs.stage_discharge`, mind the **German LfU CSV format**:
- UTF-8 **BOM** on the first line.
- **Semicolon** delimiter, **comma** decimal separator (e.g. `43,3`).
- 9 metadata lines (Quelle, station name/no., Gewässer, gauge coordinates as `Ostwert`/`Nordwert`, `Pegelnullpunktshöhe`), a blank line, then a header row (`Datum;"...[unit]";Prüfstatus`), then `"YYYY-MM-DD HH:MM";value;Geprueft` rows.
- Times are MEZ (CET). Stage is in **cm**; convert to m and add the gauge zero-point height for absolute water-surface elevation.

Read with `pandas`: `pd.read_csv(path, sep=';', decimal=',', skiprows=10, encoding='utf-8-sig')`.

### Ground-truth & calibration data (`example-Inn/ground-truth/`)

Field measurements that become **calibration targets** for HydroBayesCal, ingested by `ground_truth.py`. The canonical input is the tidy multi-tab table (below); it is either authored by the user (`inputs.measurements`) or **compiled by hydromate** from raw sources declared in `inputs.ground_truth` (a list of `{category, kind, values, positions, join_key}`). The **hydraulics/FlowTracker path is implemented** (`kind: flowtracker` joins `.ft.sum` values to a DGPS position layer; `kind: points` reads a layer that already carries quantities). **Still to build:** the sediment/GSD adapter (→ GAIA `sediment_classes`) and turning the DoD into topographic-change targets.

**Positions vs. values are split across two folders, joined by `ID`/row order:**
- Measurement **positions** are point shapefiles under `example-Inn/geodata/` — and are in **different CRSs than the project's 25832, so they must be reprojected on ingest**:
  - `geodata/flowtracker2/dgps-flowtracker-day{1,2}_utm33.shp` — EPSG:**25833** (UTM 33N). Columns `ID, E_25833, N_25833, WaterDepth, z` (z = bed elevation). 15 points (day1) / 30 (day2).
  - `geodata/sediment/MultiPAC-Mar2020-KB15.shp` — EPSG:**32632** (WGS84/UTM 32N). `name` = Surface/Subsurface. NB its `x`/`y` *attribute* columns are swapped vs the geometry — trust the geometry.
- Measured **values** live under `example-Inn/ground-truth/`:
  - **Hydraulics — `hydraulics/FlowTracker2-KB15-day{1,2}.xlsx`**: SonTek FlowTracker2 `.ft.sum` exports for KB15. One sheet; row 0 = metadata (total `DISCHARGE` [CMS], reference gauge), row 1 = headers, row 2 = units, rows 3+ = one vertical each (joins 1:1 by `ID` to the DGPS shp). Key columns: `MeasD`/`FinalD` depth [m]; `VelX/VelY/VelZ` [m/s] (+ `VxErr/VyErr/VzErr`) → velocity components / scalar velocity.
  - **Sediment — `sediment/GSDs-Uni-Stuttgart.xlsx`**: surface & subsurface grain-size distributions (cumulative + fractional) with d65/d84; feeds GAIA `sediment_classes`.

**Target tidy model (generic, source-agnostic):** a multi-tab table — one tab per category (`hydraulics`, `sediment`, …), first 3 columns `x, y, z` (in the project CRS 25832), then quantity columns (FlowTracker hydraulics → `u, v, w, u', v', w', h`). FlowTracker is only one possible source; many ground-truth sources are conceivable, so the reader stays generic and per-source adapters feed it.

**Topographic change**: the DEM-of-Difference (`dem.dem_of_difference`, dem-2020 vs DEM-2025-20cm) is the bed-change ground truth for the GAIA / topographic-change calibration; the DoD raster is produced but not yet turned into HydroBayesCal targets.

**xlsx gotcha:** these files break `openpyxl`'s style parser (`PatternFill ... unexpected keyword 'extLst'`), so `pandas.read_excel` fails on every available env. Read them by unzipping the workbook and parsing `xl/worksheets/sheet1.xml` + `xl/sharedStrings.xml` directly (the `.ft.sum` layout is stable), or with a defensive openpyxl wrapper — don't assume `read_excel` works. `openpyxl` is also **not** in `hydromate-env`.

## Working conventions

- Large rasters (the DEMs/orthos run to hundreds of MB) — don't open them blindly; use `gdalinfo`/`rasterio` to read headers, and window/downsample for any processing.
- Keep raw input data immutable. Generated artifacts go under the configured `model_dir` (e.g. `config/case/`), not mixed into `geodata/`.
- This **is** a git repository (default branch `main`). `geodata/` and `stage-discharge/` are large/local and not part of the tracked source tree.
