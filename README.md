# Inn TELEMAC setup workflow (`hydromate`)

Automated setup of a calibration-ready **TELEMAC-2D** (optionally **+ GAIA** morphodynamics) case for a reach of the **Inn river (Bavaria)**, wired into [HydroBayesCal](https://github.com/Ecohydraulics/hydrobayescal) for surrogate-assisted Bayesian calibration with quantified uncertainty.

You provide geodata + hydraulics; `hydromate` produces a ready TELEMAC case (geometry `.slf`, `boundaries.cli`, steering `.cas`, friction `.tbl`) plus a `measurements-calibration.csv` and a HydroBayesCal `config_Telemac.py`.

## Documentation

Full docs (Sphinx) live in `docs/`. Build the HTML site and open it locally with:

```bash
pip install -r docs/requirements-docs.txt   # one-time: Sphinx + theme
make -C docs html                            # build into docs/_build/html
xdg-open docs/_build/html/index.html         # open in your browser (macOS: `open`)
```

Nothing leaves your machine. See `docs/usage.rst` for the workflow, meshing, and config reference.

## Pipeline

```
ROI DEM(s) ─▶ 1. clip to ROI ─▶ 2. gmsh mesh + bathymetry + geometry.slf
                                   │  (per-MATID sizing; FRIC_ID per node)
inflow / outflow / measurements ───┤
                                   ├─▶ 3. boundaries.cli  (inflow 5 5 5 / outflow 4 4 4 | 5 4 4 / wall 2 2 2)
                                   ├─▶ 4. steady2d.cas + friction.tbl  (zonal Manning)
                                   └─▶ 5. measurements-calibration.csv + config_Telemac.py
```

* **Stage 1** (`hydromate/dem.py`) - reproject + clip the initial DEM (and optional target DEM) to the ROI boundary; optional DEM-of-Difference for morphodynamics.
* **Stage 2** (`hydromate/mesh.py`, `selafin.py`) - triangular mesh from boundary + breaklines (anisotropic, flow-aligned in the channel) with per-MATID size fields; DEM interpolated onto nodes; friction zones written as a `FRIC_ID` variable inside the geometry SELAFIN (driven by `inputs.roughness_zones` when set - the polygon `Zone ID` becomes `FRIC_ID` and the table ks becomes the `BOTTOM FRICTION` variable). A **quality report** (`hydromate/mesh_quality.py`) is logged for every build: per-region (channel vs floodplain) internal angles, aspect ratio and skewness; the **shortest edge** (critical - it bounds the CFL-limited adaptive time step); and adjacent-cell area jumps (smooth-transition check). The channel's intended elongation is reported but exempt from shape warnings; invalid geometry (zero-area, inverted, duplicate nodes, non-manifold edges) aborts the build.
* **Stage 3** (`hydromate/boundary.py`) - classify each contour node against the `inputs.liquid_boundaries` lines (a line layer whose `Type (inflow/outflow)` field tags every line `inflow` or `outflow`; the lines must coincide with the mesh-zone outer bounds). Inflow nodes get a prescribed-Q boundary (`5 5 5`); outflow nodes get a prescribed-elevation boundary (`5 4 4`) whose water level comes from the `inputs.stage_discharge` rating curve at the simulated Q (`outflow_condition: stage_discharge`, the **default**), a fixed `prescribed_elevation` (`outflow_condition: elevation`), or a free/Neumann boundary (`4 4 4`, `outflow_condition: free`); every other outer node is a solid wall (`2 2 2`). If the total inflow- and outflow-node counts differ by more than ~10%, a **stability-risk warning** is logged (rebalance via mesh resolution or line lengths).
* **Stage 4** (`hydromate/steering.py`) - friction `.tbl` (one row per friction zone, perturbed by HydroBayesCal) and the `.cas`; GAIA `.cas` when morphodynamics on. Zones come from `friction.zones` (MATID) or, for the Inn case, are derived from the roughness table (`<Zone ID> NIKU <ks> NULL`) so they match the geometry's per-node `FRIC_ID`.
* **Stage 5** (`hydromate/calibration.py`) - calibration CSV from measurements and the HydroBayesCal `config_Telemac.py`.

**Logging** - each script/phase writes a compound, timestamped `hydromate.log` into its own output folder (the build by `preprocessing.py`/`initial_run.py` -> `tm-simulation/simulation/`, the mesh-convergence study -> `tm-simulation/mesh-convergence/`), capturing all actions, the elapsed time of each calculation step (`START`/`DONE ... in N.NNs`), and every warning and error. The console mirrors it; pass `-v` for DEBUG.

**Workflow (run in order):** 1. `preprocessing.py` builds the complete TELEMAC case into `tm-simulation/simulation/` (final mesh, `.cli`, `.tbl`, `.cas`, plus the synthesised inflow + outflow rating curve); 1b. `initial_run.py` test-runs exactly that case once to confirm it does not crash (this ends preprocessing); 2. `mesh_convergence_study.py` runs the grid-independence study; 3. `run_Bayes_cal.py` calibrates the built case with HydroBayesCal against the FlowTracker velocity ground truth (the velocity measured at 0.6·h ≈ depth-averaged, compared to `SCALAR VELOCITY`), writing the calibration artifacts into `tm-simulation/calibration-validation/`.

**Mesh-convergence study** (`hydromate/convergence.py`, step 2) - a grid-independence check: the same steady simulation at a constant discharge on five meshes (the configured baseline plus two coarser at +40%/+20% cell size and two finer at -20%/-40%, each with TELEMAC's variable time step for its own CFL-admissible dt), sampling water depth and scalar velocity at the ground-truth probe points and reporting the relative change between successive refinements, an observed order of convergence and a Grid Convergence Index against a tolerance (default 2%). It writes a **styled `.xlsx` report** to `tm-simulation/postprocessing/` with a **recommended cell size** balancing grid independence against compute time. Results are read back with a small SELAFIN reader (`selafin.read_slf`).

## Install

The case-build pipeline runs in its **own** environment (`hydromate-env`); it does *not* import TELEMAC's Python. Instead it **sources** the TELEMAC `pysource.*.sh` (set in the config) whenever the solver or SELAFIN tooling is needed.

```bash
mamba env create -f environment.yml
mamba activate hydromate-env
pip install -e .
```

## Case layout

Each case lives in its own folder under `cases/<case-name>/`. To start a new case, copy the **`cases/case-template/`** scaffold (config + scripts + `USAGE.info`, no data) to `cases/<your-case>/`, drop your data into `user-sources/`, and edit `case-config.yml`. The worked, filled-in example is `cases/example-Inn/`:

```
cases/example-Inn/
  case-config.yml      # the case configuration (tracked)
  preprocessing.py            # step 1: build the full TELEMAC case into simulation/ (tracked)
  initial_run.py              # step 1b: test-run the built case (tracked)
  mesh_convergence_study.py   # step 2: grid-independence study + xlsx report (tracked)
  run_Bayes_cal.py            # step 3: HydroBayesCal calibration (velocity ground truth) (tracked)
  user-sources/        # your large source data - DEMs, GeoPackages, ground truth (gitignored)
  tm-simulation/       # produced artifacts, by workflow phase (gitignored):
    preprocessing/         # DEM clips, meshes, ground-truth table + its hydromate.log
    simulation/            # the TELEMAC case (geometry.slf, .cli, .cas, .tbl, results) + its hydromate.log
    postprocessing/        # general post-processing
    mesh-convergence/      # convergence study: mesh-convergence.xlsx/.txt, per-mesh runs, log
    calibration-validation/  # HydroBayesCal artifacts (measurements-calibration.csv, config_Telemac.py)
```

Config paths resolve relative to `case-config.yml`, so `user-sources/...` points at your data and the build writes into `tm-simulation/`. Each script logs into its own output folder (`preprocessing/`, `simulation/`, `postprocessing/`). Only the config, scripts and docs are version-controlled; `user-sources/` and `tm-simulation/` stay out of git (they run to gigabytes - see the 20 MB CI guard in `.github/workflows/`).

## Use

1. Edit `cases/example-Inn/case-config.yml` - point `telemac.pysource` at your TELEMAC env, set the input paths, mesh sizes, friction zones, and calibration parameters/ranges.
2. Provide a **ROI boundary polygon** (`inputs.boundary`): a closed polygon (or closed polyline) delineating the maximum wetted extent, in EPSG:25832.
3. Provide the **liquid boundaries** (`inputs.liquid_boundaries`): a line layer in EPSG:25832 whose `Type (inflow/outflow)` field tags each line `inflow` or `outflow` (several of each are allowed). Draw them **exactly along the mesh-zone outer bounds** so contour nodes land on them. Keep the inflow and outflow lines a similar length relative to the mesh resolution so they carry comparable node counts (within ~10%), or the build logs a stability-risk warning.
4. **Outflow stage-discharge curve** (`inputs.stage_discharge`, default `outflow_condition: stage_discharge`): a `Q,WSE` CSV that sets the downstream water level at the simulated discharge (one Q-h pair at the steady Q is enough). You don't have to make one - `preprocessing.py`/`mesh_convergence_study.py` **synthesise it** from the geodata if it is missing (width from the outflow boundary line, bed + reach slope from the DEM, trapezoidal banks, roughness from `friction.boundary_*`). To make your own, `hydromate rating -o user-sources/geodata/rating-curve.csv --strickler 38 --slope <S0> --width <b> --side-slope 1 --bed-elevation <z> --q 47`. (Alternatively set `outflow_condition: elevation` with a fixed `prescribed_elevation`, or `free` for a Neumann outflow.)
5. Run the workflow, in order:

```bash
# step 1 - build the complete TELEMAC case into tm-simulation/simulation/
python cases/example-Inn/preprocessing.py
# step 1b - test-run the built case once (confirms it does not crash; ends preprocessing)
python cases/example-Inn/initial_run.py
# step 2 - mesh-convergence study -> styled xlsx report + recommended cell size
python cases/example-Inn/mesh_convergence_study.py
# step 3 - Bayesian calibration with HydroBayesCal (velocity ground truth)
python cases/example-Inn/run_Bayes_cal.py            # --prepare-only writes CSV+config without launching
```

   (A one-shot build without the scripts: `hydromate cases/example-Inn/case-config.yml` - or `--check` to validate, `--dry-run` to also run the solver once.)
6. **Step 3 - calibrate** (in the HydroBayesCal clone, with its env):

```bash
cd cases/example-Inn/tm-simulation/calibration-validation
python /home/schwindt/github/hydrobayescal/bal_telemac.py --config config_Telemac.py
```

## Configuration reference

See `cases/example-Inn/case-config.yml` for a fully commented example. Key sections: `project` (name, CRS, output dirs), `telemac` (pysource, solver, processors), `inputs` (DEMs, boundary, breaklines, region/MATID points, liquid boundaries, inflow, optional stage-discharge + measurements), `mesh`, `friction` (zones ↔ MATID), `hydrodynamics`, optional `morphodynamics` (GAIA), and `calibration`.

### Calibration parameter naming (HydroBayesCal convention)

| Prefix                              | Target                              |
|-------------------------------------|-------------------------------------|
| `zone<MATID>`                       | bed-friction coefficient of a zone  |
| `gaiaCLASSES SHIELDS PARAMETERS <n>`| GAIA critical Shields, sediment `n` |
| `vg_zone<MATID>-<p>`                | vegetation friction parameter       |
| any literal TELEMAC keyword         | written straight into the `.cas`    |

## Coordinate system

All inputs/outputs are **EPSG:25832 (ETRS89 / UTM 32N)**, metres - see `CLAUDE.md`. Inputs in another CRS are reprojected on ingest.

## Status

v1 covers the **2D hydraulic** path end to end (friction-zone calibration against water depth / velocity). GAIA morphodynamics and the DEM-of-Difference topographic-change calibration are wired as extension points (`morphodynamics` config block, `dem.dem_of_difference`, GAIA `.cas` writer) and built out next.
