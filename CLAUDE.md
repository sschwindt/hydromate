# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A TELEMAC hydro-morphodynamic model of a reach of the **Inn river in Bavaria, Germany**. The goal is a 2D depth-averaged hydraulic + sediment-transport simulation (TELEMAC-2D coupled with GAIA), set up automatically and calibrated with quantified uncertainty via [HydroBayesCal](https://github.com/Ecohydraulics/hydrobayescal).

**Overarching aim:** largely automate *both* sides of the workflow around HydroBayesCal - **pre-processing** (geodata → ready TELEMAC case) and **post-processing** (field **ground-truth** → calibration targets + result extraction). Pre-processing (stages 1–5) is built for the 2D hydraulic path; the **post-processing / ground-truth ingestion is the active frontier** (turning FlowTracker velocity/depth, grain-size distributions, and DEM-of-Difference topographic change into the calibration-points CSV and GAIA inputs HydroBayesCal consumes). See "Ground-truth & calibration data" below.

The tracked repository is the **`hydromate` package** (`src/hydromate/`) plus a data-free **`cases/case-template/`** scaffold (config + the three workflow scripts + `USAGE.info` + `user-sources/README.md`, the starting point for a new case) and the worked example case under `cases/example-Inn/` (its `case-config.yml`, `preprocessing.py`, `initial_run.py`, `mesh_convergence_study.py`, `run_Bayes_cal.py`). Each case lives in `cases/<name>/` and keeps only those tracked files; the real Inn **input data** (DEMs, orthos, zone GeoPackages, gauge data, ground truth) is large (~5 GB) and lives in `cases/example-Inn/user-sources/` for co-development only, while produced artifacts land in `cases/example-Inn/tm-simulation/`, split by workflow phase: `preprocessing/` (DEM clips, meshes, ground-truth table), `simulation/` (the TELEMAC case build + results), `postprocessing/` (general post-processing), `mesh-convergence/` (the grid-independence study), `calibration-validation/` (HydroBayesCal artifacts). Each phase's script writes its outputs and its `hydromate.log` into its own subfolder. Both `user-sources/` and `tm-simulation/` are **gitignored** (`.gitignore` ignores `cases/*/user-sources/` and `cases/*/tm-simulation/`, plus a safety net of binary/geo extensions `*.tif/*.gpkg/*.shp/*.xlsx/*.slf/*.log/...`); a CI workflow (`.github/workflows/file-size-limit.yml`) additionally fails any push/PR with a tracked file over 20 MB. Don't assume the data files are present in a fresh checkout. See `README.md` for the workflow and `cases/example-Inn/case-config.yml` for the case config (a commented template; all paths resolve relative to the config file's directory).

## The `hydromate` workflow

`hydromate cases/example-Inn/case-config.yml` runs a 5-stage pipeline (`src/hydromate/pipeline.py`):

1. `dem.py` - reproject + clip the initial (and optional target) DEM to the ROI boundary.
2. `mesh.py` + `selafin.py` - gmsh triangular mesh; DEM interpolated onto nodes; geometry written as SELAFIN with a `FRIC_ID` per-node variable (so no separate ZONES FILE is needed). The geometry is written in **double precision** (SERAFIND); single precision collapses sub-metre cells on UTM coordinates (see `selafin.py` design point). `build_mesh` prunes gmsh's geometry/1D-only nodes to triangle-referenced nodes (no orphans/duplicates), enforces consistent CCW winding, then runs `mesh_quality.assess_quality` (logged via `log_report`, stored on `Mesh.quality`). The mesh boundary is walked to build IPOBO, which **must** stay consistent with the `.cli` row order. **Two meshing strategies** (auto-selected): *anisotropic* (when `inputs.mesh_zones` + `inputs.channel_centerline` are set) types each mesh-zone polygon `channel`/`floodplain`/`refinement` (by `Zone Name` substring) and sizes it by its own `Max Edge Length (m)` field - channel cells elongated along the centerline, floodplain/refinement near-equilateral - via a metric-tensor background view + gmsh **BAMG** (`Mesh.Algorithm=7`); *isotropic fallback* uses `default_size`/`breakline_size`/`region_sizes` distance fields. See "Meshing" below.
3. `boundary.py` - classify contour nodes against the liquid-boundary lines and write the `.cli`. The lines come from `inputs.liquid_boundaries` (a line layer whose `Type (inflow/outflow)` field - note the Inn file's typo `Type (inflow/outlfow)`, still detected - tags each line `inflow`/`outflow`; several of each allowed) and must coincide with the mesh-zone outer bounds. Codes: solid wall `2 2 2` (every non-liquid outer node), inflow `5 5 5` (prescribed Q), outflow `5 4 4` prescribed-elevation (the default `hydrodynamics.outflow_condition: stage_discharge`, WSE from the rating curve at the simulated Q; or `elevation` for a fixed WSE) or `4 4 4` free/Neumann (`outflow_condition: free`, needs no WSE). Matching tolerance is tied to the local edge length (anisotropic: ~2×`floodplain_size`). If total inflow- vs outflow-node counts differ by >10%, a stability-risk warning is logged.
4. `steering.py` - friction `.tbl` (one row per friction zone: `<id> <LAW> <coef> NULL`, ends with `END`; comments only on `*` lines - rows come from `friction.zones`/MATID or, failing that, the roughness table as `<Zone ID> NIKU <ks> NULL`) and the Telemac2d `.cas`; GAIA `.cas` when enabled. The prescribed inflow Q comes from `hydraulics.py` (reads `inputs.inflow`). The outflow water-surface elevation is needed for the prescribed-elevation conditions: the default `hydrodynamics.outflow_condition: stage_discharge` interpolates `inputs.stage_discharge` (a Q-h CSV; one pair suffices for steady) at the simulated Q, while `elevation` uses `hydrodynamics.prescribed_elevation`; `free` prescribes nothing. When no measured rating curve exists, `hydromate.rating.generate_stage_discharge` (CLI: `hydromate rating`) synthesises one from a Manning `n`/Strickler `Kst` and a trapezoidal channel geometry by inverting Manning's normal-flow equation; `rating.synthesize_outflow_rating(cfg, Q)` derives the geometry from the case data (width = outflow liquid-boundary line length, bed = DEM thalweg along it, slope = reach slope from the DEM along the centerline, roughness = `friction.boundary_*`) and `cases/example-Inn/preprocessing.py` (and the convergence-study script) call it to auto-generate `inputs.stage_discharge` when missing. The lateral-boundary friction keywords (`LAW OF FRICTION ON LATERAL BOUNDARIES` / `ROUGHNESS COEFFICIENT OF BOUNDARIES`) come from `friction.boundary_law` / `boundary_coefficient` (Inn: Strickler 38).
5. `ground_truth.py` + `calibration.py` - compile the configured `inputs.ground_truth` sources into the **tidy multi-tab table** (`ground_truth.compile_ground_truth`, written to `preprocessing_dir/ground-truth.xlsx` unless `inputs.measurements` overrides), then turn it into `measurements-calibration.csv` and the HydroBayesCal `config_Telemac.py` (both in `calibration_dir` = `calibration-validation/`).

`pipeline.run()` returns an `Artifacts` dataclass listing every produced path; `cli.py` logs these. The package's public API (`hydromate/__init__.py`) exports `Config`, `load_config`, `clip_to_roi`, `clip_dem_to_roi`, `compile_ground_truth`, `read_tidy`, the mesh helpers `build_mesh` / `channel_node_mask` / `interpolate_elevations` / `interpolate_roughness` / `write_mesh` / `assess_quality`, the rating-curve helpers `generate_stage_discharge` / `normal_depth` / `synthesize_outflow_rating`, the mesh-convergence helpers `run_mesh_convergence` / `percent_levels`, and the logging helpers `setup_logging` / `log_step` / `logging_to`. `mesh.build_mesh(cfg)` (no DEM) yields a bare mesh; `interpolate_elevations(mesh, dem, decimals=4)` fills the bottom from a raster (rounded); `write_mesh(mesh, path)` writes a geometry SELAFIN. These low-level helpers let you build a mesh programmatically; the worked example's `preprocessing.py` instead drives the full `pipeline.run` build (writing `geometry.slf` etc. into `simulation/`; see the workflow below).

### Meshing (stage 2)

The mesher is **gmsh**, chosen over the SALOME/Q4TS route (which the working Inn model historically used) because gmsh is pip-installable, fully headless/scriptable, and supports the anisotropic meshing needed here - whereas Q4TS runs inside SALOME's GUI/SMESH stack and is heavyweight/version-sensitive to automate. SALOME lives at `/home/schwindt/opt/salome/` if ever needed.

- **Anisotropic, flow-aligned channel** (the requirement): channel triangles are *elongated along the channel centerline*, floodplain triangles near-equilateral, with a smooth transition. Implemented exactly like gmsh tutorial **t17**: build a coarse background triangulation (a Delaunay grid over the ROI bbox, node-budget-capped ~40k so any domain fits in memory), assign each background node a **3×3 metric tensor** (eigenvalue = 1/h²; anisotropic `1/(channel_size·anisotropy)²` along the local centerline tangent and `1/channel_size²` across, inside `channel` zones; isotropic `1/floodplain_size²` elsewhere), push it as a `TT` list **PostView** + `setAsBackgroundMesh`, then mesh with **`Mesh.Algorithm=7` (BAMG)**, `Mesh.SmoothRatio=growth_ratio`, `Mesh.AnisoMax`. The coarse background also produces the smooth channel→floodplain blend (gmsh interpolates the metric within each background triangle).
- Zone selection is by **substring** of the `Zone Name` attribute (case-insensitive): `*channel*` vs `*floodplain*` in `inputs.mesh_zones`. The centerline (`inputs.channel_centerline`) is sampled and `np.gradient` gives per-vertex tangents (nearest via `cKDTree`).
- BAMG **approximates** the metric: realised cross-channel size and anisotropy come out a bit gentler than the configured targets, and `growth_ratio` (default 1.2) intentionally relaxes them near banks. Knobs: `mesh.channel_size` 0.5, `floodplain_size` 1.5, `growth_ratio` 1.2, `channel_anisotropy` 4.0, `max_aspect_ratio` 4.0. Defaults are **high-res**: the Inn KB15 ROI (~0.32 km², ~130 m-wide channel) yields ~720k elements / ~360k nodes (~90 s, ~0.7 GB; the aspect cap below tightens `AnisoMax` which refines the mesh) - raise the sizes while iterating.
- **Aspect-ratio cap** (`mesh.max_aspect_ratio`, default 4.0 = longest:shortest edge): BAMG overshoots the metric anisotropy in the tail (~1.6x) and leaves a few channel slivers, so `Mesh.AnisoMax` is set to `max_aspect_ratio/1.6` and a post-mesh **edge-flip** pass (`_flip_sharp_edges`) flips the shared (long) edge of over-sharp cell pairs - a topological repair that touches only the offending cells (boundary untouched, IPOBO/winding preserved, no inversions), bringing the realised channel max from ~6.5:1 down to ≤~4:1 while keeping the bulk elongation (channel aspect median ~2.0 vs floodplain ~1.4).
- The **isotropic fallback** (no mesh_zones/centerline) is the original distance-field path (`default_size`/`breakline_size`/`region_sizes`); the integration test exercises it.
- **Quality assessment** (`mesh_quality.py`, vectorised, scales to the ~525k-element mesh): logs per-region (channel vs floodplain, via a centroid-in-`_channel_union` mask) internal angles, aspect ratio, skewness; the **shortest edge** (flagged as CRITICAL for the CFL-limited adaptive time step); and the fraction of adjacent-cell area jumps over `mesh.max_area_jump` (20%). The channel is `relaxed=True` so its *intended* anisotropy (aspect ≫ 1) is reported but never warned on - shape warnings (angle < `min_angle_deg`/> `max_angle_deg`, high aspect) apply to the floodplain only. Fatal defects (zero-area, inverted/mixed-orientation, duplicate nodes, non-manifold edges) **raise**; orphan nodes, extra boundary loops (hanging gaps) and IPOBO mismatch (wrong boundary tags) warn. Thresholds are `mesh.min_angle_deg`/`max_angle_deg`/`max_area_jump`.
- Meshing (mesh-zones) and friction (roughness-zones) are **separate concerns**: `inputs.mesh_zones` only sizes the mesh. Friction zonation comes from `inputs.roughness_zones` (an integer `Zone ID` per polygon): `mesh.interpolate_roughness(cfg, mesh)` tags every node/element with the `Zone ID` it falls in (nearest polygon otherwise), writes it as the per-node `FRIC_ID` (overriding the MATID-derived ids), and stores the `roughness_table` value for that id as the `BOTTOM FRICTION` variable. `mesh.run` (hence `pipeline.run`) calls it whenever `roughness_zones` + `roughness_table` are set, so the built `geometry.slf` carries the zone `FRIC_ID` + `BOTTOM FRICTION`, and `steering.write_friction_tbl` derives the matching `.tbl` rows (`<Zone ID> NIKU <ks> NULL`, law `friction.roughness_law`, default 5) from the same table. HydroBayesCal perturbs each `ks` via `zone1`/`zone2`.

Key design points:
- The build runs in its **own** `hydromate-env` conda env (gmsh/geopandas/rasterio); it does not import TELEMAC's Python. `env.py` *sources* `telemac.pysource` (the `pysource.*.sh`) in a subshell for any solver/SELAFIN call. The config's `telemac.pysource` must point at the real script (e.g. `/home/schwindt/opt/telemac/configs/pysource.mint22.sh`).
- Friction zonation has two routes: (a) the **roughness-zones** route - `inputs.roughness_zones` (`Zone ID`) + `inputs.roughness_table` (id→ks) drive the per-node `FRIC_ID` and `BOTTOM FRICTION` via `interpolate_roughness` (preferred for the Inn case; see the Meshing section); (b) the older **MATID** scheme (1 riverbed_fine … 5 floodplain; see table below) carried by `inputs.region_points` + `inputs.region_table` and/or `friction.zones` in the config → friction `.tbl` rows and `zone<MATID>` calibration parameters. Either way HydroBayesCal perturbs the coefficient column of the `.tbl`. `steering.write_friction_tbl` builds the `.tbl` in priority order: explicit `friction.zones` (MATID) > the roughness table (one `<Zone ID> NIKU <ks> NULL` row each) > a single default zone - so the `.tbl` rows always match the geometry's per-node `FRIC_ID`.
- `src/hydromate/selafin.py` is a self-contained big-endian SELAFIN writer (plus a `read_slf` reader for results); its output is validated against TELEMAC's own `data_manip.formats.selafin.Selafin` reader in the tests.
- **Logging** (`src/hydromate/logsetup.py`): each script/phase writes a compound, timestamped `hydromate.log` into its **own output folder** (`setup_logging(path)` routes the `hydromate` logger and captured Python warnings to it; file at DEBUG, console at the chosen level, append mode). the build (`pipeline.run`, called by `preprocessing.py`; and `initial_run.py`'s test run) -> `model_dir` (`simulation/`), the mesh-convergence study -> `mesh-convergence/` (scoped via the `logging_to(path)` context manager so it doesn't capture the per-level builds elsewhere). `log_step(name)` logs `START`/`DONE … in N.NNs` (or `FAILED … after N.NNs`) - wrapped around every pipeline stage and the heavy mesh/convergence sub-steps for per-step timing. `pipeline.run(..., log_to_file=False)` skips its own logfile (the convergence study's per-level builds use this so they log only to `mesh-convergence/hydromate.log`). Tests reset handlers via an autouse fixture (`tests/conftest.py`).
- **Pre-wetting** (warm start): the installed TELEMAC has **no keyword** to seed an initial water depth on a polygon, so `hydrodynamics.prewet_depth` (m) instead drives a warm start. When set, stage 4 writes an initial-conditions SELAFIN (`cfg.ic_slf`, `initial-conditions.slf` in `model_dir`) carrying `VELOCITY U/V = 0` and `WATER DEPTH = prewet_depth` on the nodes inside the `*channel*` mesh-zones (dry elsewhere, on the geometry's own mesh) via `selafin.write_initial_state` + `mesh.channel_node_mask`; the `.cas` then continues from it (`COMPUTATION CONTINUED : YES`, `PREVIOUS COMPUTATION FILE`, `INITIAL TIME SET TO ZERO : YES`) instead of the analytical `INITIAL CONDITIONS`. This avoids advancing the wetting front from a dry bed (a large time saving) and is what the mesh-convergence study uses (`INITIAL_DEPTH`, default 0.5 m). `pipeline.run` records the file in `Artifacts.initial_conditions`.
- Calibration CSV schema (read by HydroBayesCal): `id, x, y, z, <QTY>_DATA, <QTY>_ERROR`, where `<QTY>` is a SELAFIN name (e.g. `WATER DEPTH`, `SCALAR VELOCITY`).

v1 covers the **2D hydraulic** path end to end; GAIA morphodynamics and the DEM-of-Difference topographic-change calibration are wired as extension points (`morphodynamics` config block, `dem.dem_of_difference`, `steering.write_gaia_cas`).

### Build, test, run

```bash
mamba env create -f environment.yml && mamba activate hydromate-env && pip install -e ".[dev,gui]"
hydromate cases/example-Inn/case-config.yml --check        # validate config only
hydromate cases/example-Inn/case-config.yml                # build the case
hydromate cases/example-Inn/case-config.yml --dry-run      # build, then run the solver once to validate
hydromate cases/example-Inn/case-config.yml --no-validate-env  # build without sourcing the TELEMAC env
hydromate clip <raster> -b <boundary> -o <out>   # crop one raster to a ROI, no config
hydromate rating -o <out.csv> --manning <n> --slope <S0> --width <b> --q <Q...>  # normal-flow outflow rating curve
mamba run -n hydromate-env pytest tests/ # end-to-end test on synthetic fixtures (no solver)
mamba run -n hydromate-env pytest tests/test_integration.py::<name>  # single test
mamba run -n hydromate-env ruff check src/   # lint (line-length 100)
```

Packaging is `pyproject.toml` (setuptools, `src/` layout); extras: `[dev]` = pytest+ruff, `[gui]` = streamlit. Entry points: `hydromate` (CLI) and `hydromate-gui` (Streamlit config editor - `gui/launch.py` shells out to `streamlit run gui/app.py`).

`cases/example-Inn/` holds the worked example for the Inn case, run against its `case-config.yml` as flat scripts (`mamba run -n hydromate-env python cases/example-Inn/<script>.py`). The **workflow is three ordered steps**: (1) `preprocessing.py` builds the complete TELEMAC case into `tm-simulation/simulation/` (via `pipeline.run`, at a constant Q=47 m³/s, synthesising the inflow + outflow rating if missing) - the final mesh `geometry.slf`, `boundaries.cli`, `friction.tbl`, `steady2d.cas`, plus the HBC artifacts in `calibration-validation/`; (1b) `initial_run.py` test-runs *exactly that* `steady2d.cas` once (`TelemacRuntime.run_solver`, no rebuild) to confirm it does not crash, which ends preprocessing; (2) `mesh_convergence_study.py` runs the grid-independence study; (3) `run_Bayes_cal.py` calibrates the built case with HydroBayesCal. Step 3 compiles the FlowTracker velocity ground truth into the calibration-points CSV (the point velocity, measured at **0.6·h**, approximates the depth-averaged velocity, so it is compared to TELEMAC `SCALAR VELOCITY` = sqrt(U²+V²) built from the **horizontal** components only), ensures the `.cas` outputs `SCALAR VELOCITY` (graphic-printout `M`), emits `config_Telemac.py`, and launches HydroBayesCal's `bal_telemac.py` in its own checkout/env (`HYDROBAYESCAL_DIR` / `HYDROBAYESCAL_ENV`, defaulting to `/home/schwindt/github/hydrobayescal` and `wrr-proj`; `--prepare-only` writes the CSV+config without launching) - everything into `calibration-validation/`. HydroBayesCal reads the CSV's first four columns by position (id, x, y, z) and `<QTY>_DATA`/`<QTY>_ERROR` by name; `_ERROR` is a site-specific error in physical units added in quadrature to its built-in 10% measurement + 10% surrogate error.

The mesh-convergence study (`hydromate/convergence.py`, step 2; `cases/example-Inn/mesh_convergence_study.py`, which creates and works in its own `tm-simulation/mesh-convergence/` folder) runs the same steady sim on **five meshes** (`percent_levels`: configured baseline plus +40%/+20%-coarser and -20%/-40%-finer cell sizes) at constant discharge with TELEMAC's variable time step (per-mesh CFL dt), samples water depth + scalar velocity at the ground-truth probe points (`default_probes`), and reports the successive relative change, observed order p and GCI vs a tolerance (default 2%). Every per-level build is **pre-wetted** (`INITIAL_DEPTH`, default 0.5 m -> `hydrodynamics.prewet_depth`): the channel mesh-zone nodes are warm-started with that water depth so the solver need not advance the wetting front from a dry bed (see "Pre-wetting" below). `ConvergenceReport.to_xlsx()` writes a **styled .xlsx** (openpyxl) to `mesh-convergence/` and `recommendation()` picks the **coarsest grid-independent cell size** (convergence vs. compute-time tradeoff, with the runtime speed-up vs. the finest). The TELEMAC run is injectable (`simulate=`) so the report maths/xlsx are unit-tested without the solver. Results SELAFINs are read with the minimal `selafin.read_slf` (the writer's counterpart; single/double precision, multi-frame, returns the last frame).

A build needs a **ROI boundary polygon** (`inputs.boundary`, a closed polygon/polyline in EPSG:25832; the Inn example uses `cases/example-Inn/user-sources/geodata/roi-kb15.gpkg`) and `inputs.liquid_boundaries` (both set in the Inn config). `inputs.inflow` and `inputs.stage_discharge` are **auto-synthesised** by `preprocessing.py` when missing (a constant Q=47 inflow series and a normal-flow rating curve), so the only machine-specific thing to set for a build is `telemac.pysource`.

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

Note: GDAL is **not** in `wrr-proj` by default in all cases - if `from osgeo import gdal` fails, prefer the TELEMAC pysource environment (which ships GDAL) or `rasterio`.

## Coordinate system - non-negotiable

**Everything is EPSG:25832 (ETRS89 / UTM Zone 32N), units in metres.** This was verified for the DEMs, all shapefiles, and the gauge metadata. Any new raster, mesh, or shapefile must use the same CRS, or downstream alignment (DEM interpolation onto the mesh, breakline snapping) will silently break.

Vertical datum varies across the gauge data (DHHN12 / DHHN2016 / "m NN" vs "m NHN") - check and reconcile datum before using gauge zero-point heights as model boundary elevations.

## Data layout and meaning

The Inn co-development dataset lives under `cases/example-Inn/user-sources/geodata/` (gitignored). The exact files evolve; what `cases/example-Inn/case-config.yml` currently points at:
- `dem-2020.tif` - baseline terrain (0.5 m DEM), `inputs.dem_initial`.
- `DEM-2025-20cm.tif` - 0.20 m high-res survey, `inputs.dem_target` (enables the morphodynamic/DoD path).
- `roi-kb15.gpkg` - the ROI / max-wetted-extent polygon (`inputs.boundary`), subreach KB15.
- `liquid-boundaries.gpkg` - inflow/outflow boundary segments (drives the `.cli`).
- `mesh-zones.gpkg` - polygons with a `Zone Name` (`channel`/`floodplain`) driving the anisotropic mesh (`inputs.mesh_zones`). `channel-centerline.gpkg` - the line channel cells are elongated along (`inputs.channel_centerline`).
- `roughness-zones.gpkg` - polygons with an integer **`Zone ID`** (1 = channel, 2 = floodplain) driving the friction zonation (`inputs.roughness_zones`); `roughness-table.csv` (`zone_id,ks`, e.g. `1,0.2` / `2,0.5`) maps each id to a Nikuradse ks (`inputs.roughness_table`). `interpolate_roughness` writes these as the per-node `FRIC_ID` + `BOTTOM FRICTION` and `steering` makes the matching `.tbl` (`<id> NIKU <ks> NULL`).
- `rating-curve.csv` - outflow `Q,WSE` stage-discharge curve (`inputs.stage_discharge`); auto-synthesised from the geodata by `preprocessing.py` if absent.
- `iws-uas/`, `flowtracker2/`, `sediment/`, `muehldorf-gauge-XS/` - ortho/UAS imagery, FlowTracker velocity ground truth, GSDs, and gauge cross-section. `cases/example-Inn/user-sources/ground-truth/` holds the calibration ground-truth spreadsheets.

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

The upstream (discharge) and downstream (water level) boundaries are driven by gauge exports from the Bavarian LfU (gkd.bayern.de) - discharge [m³/s] at Kraiburg (Q ≈ 47 m³/s for the Nov-2020 reference period), stage [cm] at Wasserburg (upstream) and Mühldorf (downstream). When such CSVs are supplied via `inputs.inflow` / `inputs.stage_discharge`, mind the **German LfU CSV format**:
- UTF-8 **BOM** on the first line.
- **Semicolon** delimiter, **comma** decimal separator (e.g. `43,3`).
- 9 metadata lines (Quelle, station name/no., Gewässer, gauge coordinates as `Ostwert`/`Nordwert`, `Pegelnullpunktshöhe`), a blank line, then a header row (`Datum;"...[unit]";Prüfstatus`), then `"YYYY-MM-DD HH:MM";value;Geprueft` rows.
- Times are MEZ (CET). Stage is in **cm**; convert to m and add the gauge zero-point height for absolute water-surface elevation.

Read with `pandas`: `pd.read_csv(path, sep=';', decimal=',', skiprows=10, encoding='utf-8-sig')`.

### Ground-truth & calibration data (`cases/example-Inn/user-sources/ground-truth/`)

Field measurements that become **calibration targets** for HydroBayesCal, ingested by `ground_truth.py`. The canonical input is the tidy multi-tab table (below); it is either authored by the user (`inputs.measurements`) or **compiled by hydromate** from raw sources declared in `inputs.ground_truth` (a list of `{category, kind, values, positions, join_key}`). The **hydraulics/FlowTracker path is implemented** (`kind: flowtracker` joins `.ft.sum` values to a DGPS position layer; `kind: points` reads a layer that already carries quantities). **Still to build:** the sediment/GSD adapter (→ GAIA `sediment_classes`) and turning the DoD into topographic-change targets.

**Positions vs. values are split across two folders, joined by `ID`/row order:**
- Measurement **positions** are point shapefiles under `cases/example-Inn/user-sources/geodata/` - and are in **different CRSs than the project's 25832, so they must be reprojected on ingest**:
  - `geodata/flowtracker2/dgps-flowtracker-day{1,2}_utm33.shp` - EPSG:**25833** (UTM 33N). Columns `ID, E_25833, N_25833, WaterDepth, z` (z = bed elevation). 15 points (day1) / 30 (day2).
  - `geodata/sediment/MultiPAC-Mar2020-KB15.shp` - EPSG:**32632** (WGS84/UTM 32N). `name` = Surface/Subsurface. NB its `x`/`y` *attribute* columns are swapped vs the geometry - trust the geometry.
- Measured **values** live under `cases/example-Inn/user-sources/ground-truth/`:
  - **Hydraulics - `hydraulics/FlowTracker2-KB15-day{1,2}.xlsx`**: SonTek FlowTracker2 `.ft.sum` exports for KB15. One sheet; row 0 = metadata (total `DISCHARGE` [CMS], reference gauge), row 1 = headers, row 2 = units, rows 3+ = one vertical each (joins 1:1 by `ID` to the DGPS shp). Key columns: `MeasD`/`FinalD` depth [m]; `VelX/VelY/VelZ` [m/s] (+ `VxErr/VyErr/VzErr`) → velocity components / scalar velocity.
  - **Sediment - `sediment/GSDs-Uni-Stuttgart.xlsx`**: surface & subsurface grain-size distributions (cumulative + fractional) with d65/d84; feeds GAIA `sediment_classes`.

**Target tidy model (generic, source-agnostic):** a multi-tab table - one tab per category (`hydraulics`, `sediment`, …), first 3 columns `x, y, z` (in the project CRS 25832), then quantity columns (FlowTracker hydraulics → `u, v, w, u', v', w', h`). FlowTracker is only one possible source; many ground-truth sources are conceivable, so the reader stays generic and per-source adapters feed it.

**Topographic change**: the DEM-of-Difference (`dem.dem_of_difference`, dem-2020 vs DEM-2025-20cm) is the bed-change ground truth for the GAIA / topographic-change calibration; the DoD raster is produced but not yet turned into HydroBayesCal targets.

**xlsx gotcha:** these *FlowTracker/GSD* files break `openpyxl`'s style parser (`PatternFill ... unexpected keyword 'extLst'`), so `pandas.read_excel` fails on them on every available env. Read them by unzipping the workbook and parsing `xl/worksheets/sheet1.xml` + `xl/sharedStrings.xml` directly (the `.ft.sum` layout is stable), or with a defensive openpyxl wrapper - don't assume `read_excel` works on these inputs. (`openpyxl` itself **is** in `hydromate-env` - `convergence.ConvergenceReport.to_xlsx` uses it to *write* the styled convergence report fine; the issue is only *reading* those specific styled inputs.)

## Working conventions

- Large rasters (the DEMs/orthos run to hundreds of MB) - don't open them blindly; use `gdalinfo`/`rasterio` to read headers, and window/downsample for any processing.
- Keep raw input data immutable. Generated artifacts go under the configured phase dirs in `cases/example-Inn/tm-simulation/` (preprocessing/ simulation/ postprocessing/ mesh-convergence/ calibration-validation/), not mixed into `user-sources/`.
- This **is** a git repository (default branch `main`). Per-case `user-sources/` (input data) and `tm-simulation/` (produced artifacts) are large/local and gitignored; only the package, the case config + scripts, and docs are tracked.
