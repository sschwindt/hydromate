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

**Logging** - each script/phase writes a compound, timestamped `hydromate.log` into its own output folder (the build by `preprocessing.py`/`initial_run.py` -> `hydromate-case/simulation/`, the mesh-convergence study -> `hydromate-case/mesh-convergence/`), capturing all actions, the elapsed time of each calculation step (`START`/`DONE ... in N.NNs`), and every warning and error. The console mirrors it; pass `-v` for DEBUG.

**Workflow (run in order):** 1. `preprocessing.py` builds the complete TELEMAC case into `hydromate-case/simulation/` (final mesh, `.cli`, `.tbl`, `.cas`) at the inflow Q (`hydrodynamics.prescribed_flowrate`) and outflow stage prescription read from **this case's** config - the shared `hydromate.prepare_steady_inputs` helper synthesises a constant inflow series / outflow rating only when those inputs are missing; 1b. `initial_run.py` test-runs exactly that case once to confirm it does not crash, then runs a boundary-flux convergence analysis via **pythomac** that writes the same four files as pythomac's example into `simulation/` (`extracted-fluxes.csv` + `flux-convergence.png`, `convergence-rate.csv` + `convergence-rate.png`) - this ends preprocessing; 2. `mesh_convergence_study.py` runs the grid-independence study; 3. `run_Bayes_cal.py` calibrates the built case with HydroBayesCal against the FlowTracker velocity ground truth (the velocity measured at 0.6·h ≈ depth-averaged, compared to `SCALAR VELOCITY`), writing the calibration artifacts into `hydromate-case/calibration-validation/`.

**Initial-run numerics** - the 2D case uses the finite-element kernel with an auto-selected turbulence model (k-epsilon / Smagorinski / Spalart-Allmaras by mesh resolution) and a CFL-adaptive time step. The steering ships **compute-stable defaults** so the wetting/drying steady march does not explode: target Courant 0.30 (`time_step: 0.25` is only the start step), `IMPLICITATION FOR DEPTH/VELOCITY 0.80`, `FREE SURFACE GRADIENT COMPATIBILITY 0.9`, `DISCRETIZATIONS IN SPACE 11;11`, `H CLIPPING : NO` and a raised k-epsilon solve budget; the graphic printout is `'U,V,S,B,H,M,Q,F'` plus `K,E` (TKE + dissipation) for k-epsilon. It is a **dry start**: only a thin water plug at the inflow line is seeded so the prescribed-Q boundary can establish (a fully dry bed makes TELEMAC's `DEBIMP` abort), and the rest of the domain wets from the inflow - set `hydrodynamics.prewet_depth` to warm-start the whole channel instead. With the variable time step the run is bounded by `DURATION` (`hydrodynamics.duration`, seconds), and **convergence is judged by the boundary-flux balance**, not by the unreliable steady-state auto-stop (off by default). All of this is emitted by `preprocessing.py` (via `pipeline.run`); the mesh-convergence study reuses the same numerics per mesh.

**Optional 3D extension (after the 2D path):** the 2D simulation is the foundation - a 3D run is only built *once the 2D run has produced its hotstart result* (`initial_run.py` → `r2d.slf`) and *after the 2D mesh-convergence study has settled the horizontal resolution*. Then `add3d.py` writes a non-hydrostatic TELEMAC-3D case hotstarted from the 2D result, and - because the vertical discretization `dz` (the number of sigma layers) is a **new** discretization choice that the 2D study never touched - `vertical_convergence_3d.py` runs a **second, separate grid-independence study over the number of vertical layers**, the 3D analogue of step 2.

**Mesh-convergence study** (`hydromate/convergence.py`, step 2) - a grid-independence check: the same steady simulation at a constant discharge on five meshes (the configured baseline plus two coarser at +40%/+20% cell size and two finer at -20%/-40%, each with TELEMAC's variable time step for its own CFL-admissible dt), sampling water depth and scalar velocity at the ground-truth probe points and reporting the relative change between successive refinements, an observed order of convergence and a Grid Convergence Index against a tolerance (default 2%). It writes a **styled `.xlsx` report** to `hydromate-case/postprocessing/` with a **recommended cell size** balancing grid independence against compute time. Results are read back with a small SELAFIN reader (`selafin.read_slf`).

**3D extension & vertical-layer convergence** (`hydromate/threed.py`, `vertical_convergence.py`) - optional, and strictly *after* the 2D path. A 3D run needs the converged 2D result as its hotstart, so it follows `initial_run.py`; and you only build it on a horizontal mesh whose resolution the 2D mesh-convergence study (step 2) has already vouched for - the 3D case reuses that same horizontal mesh. `add3d.py` writes `<case-name>3d.cas` (non-hydrostatic, sigma layers, with the turbulence model and an initial layer count inferred from the 2D result and the time step sized for Courant 0.6). The vertical discretization `dz` is then its **own** discretization question - varying the horizontal cell size in step 2 tells you nothing about how many vertical layers you need - so `vertical_convergence_3d.py` re-runs the 3D case over a ladder of vertical-layer counts on the same horizontal mesh and reports the relative change / observed order / GCI against `dz`, recommending the **fewest grid-independent number of layers**. Outputs go to `hydromate-case/postprocessing/vertical-convergence/`.

## Install

The case-build pipeline runs in its **own** environment (`hydromate-env`); it does *not* import TELEMAC's Python. Instead it **sources** the TELEMAC `pysource.*.sh` (set in the config) whenever the solver or SELAFIN tooling is needed.

```bash
mamba env create -f environment.yml
mamba activate hydromate-env
pip install -e .
```

### Configuration editor (GUI)

Instead of hand-editing the YAML you can fill the configuration in as a browser form (Streamlit). Install the `gui` extra and launch it:

```bash
pip install -e ".[gui]"
hydromate-gui                 # opens a local app in your browser; nothing leaves your machine
# hydromate-gui --server.port 8600   # extra args are forwarded to Streamlit
```

The form mirrors every config section (Project, TELEMAC, Inputs, Mesh, Friction, Hydrodynamics, Morphodynamics, Calibration) plus a **Workflow** tab summarising the steps; friction zones, calibration parameters, ground-truth sources and sediment classes are edited as tables. You can load an existing YAML, preview/download the generated YAML, save it, and run **Validate** (`--check`) or **Build** (the case build = workflow step 1) directly - the later steps (test run, mesh convergence, calibration, the 3D extension) are run from the per-case scripts.

## Case layout

Each case lives in its own folder under `cases/<case-name>/`. To start a new case, copy the **`cases/case-template/`** scaffold (config + scripts + `USAGE.info`, no data) to `cases/<your-case>/`, drop your data into `user-sources/`, and edit `case-config.yml`. The worked, filled-in example is `cases/example-Inn/`:

```
cases/example-Inn/
  case-config.yml      # the case configuration (tracked)
  preprocessing.py            # step 1: build the full TELEMAC case into simulation/ (tracked)
  initial_run.py              # step 1b: test-run the built case (tracked)
  mesh_convergence_study.py   # step 2: grid-independence study + xlsx report (tracked)
  run_Bayes_cal.py            # step 3: HydroBayesCal calibration (velocity ground truth) (tracked)
  add3d.py                    # optional (after 2D): non-hydrostatic 3D case, hotstart from 2D (tracked)
  vertical_convergence_3d.py  # optional (after 3D): vertical-layer (dz) convergence study (tracked)
  user-sources/        # your large source data - DEMs, GeoPackages, ground truth (gitignored)
  hydromate-case/       # produced artifacts, by workflow phase (gitignored):
    preprocessing/         # DEM clips, meshes, ground-truth table + its hydromate.log
    simulation/            # the TELEMAC case (geometry.slf, .cli, .cas, .tbl, results) + its hydromate.log
    postprocessing/        # general post-processing
    mesh-convergence/      # convergence study: mesh-convergence.xlsx/.txt, per-mesh runs, log
    vertical-convergence/  # 3D vertical-layer (dz) study: vertical-convergence.xlsx/.txt, per-level runs
    calibration-validation/  # HydroBayesCal artifacts (measurements-calibration.csv, config_Telemac.py)
```

Config paths resolve relative to `case-config.yml`, so `user-sources/...` points at your data and the build writes into `hydromate-case/`. Each script logs into its own output folder (`preprocessing/`, `simulation/`, `postprocessing/`). Only the config, scripts and docs are version-controlled; `user-sources/` and `hydromate-case/` stay out of git (they run to gigabytes - see the 20 MB CI guard in `.github/workflows/`).

## Use

1. Edit `cases/example-Inn/case-config.yml` - point `telemac.pysource` at your TELEMAC env, set the input paths, mesh sizes, friction zones, and calibration parameters/ranges.
2. Provide a **ROI boundary polygon** (`inputs.boundary`): a closed polygon (or closed polyline) delineating the maximum wetted extent, in EPSG:25832.
3. Provide the **liquid boundaries** (`inputs.liquid_boundaries`): a line layer in EPSG:25832 whose `Type (inflow/outflow)` field tags each line `inflow` or `outflow` (several of each are allowed). Draw them **exactly along the mesh-zone outer bounds** so contour nodes land on them. Keep the inflow and outflow lines a similar length relative to the mesh resolution so they carry comparable node counts (within ~10%), or the build logs a stability-risk warning.
4. **Outflow stage-discharge curve** (`inputs.stage_discharge`, default `outflow_condition: stage_discharge`): a `Q,WSE` CSV that sets the downstream water level at the simulated discharge (one Q-h pair at the steady Q is enough). You don't have to make one - `preprocessing.py`/`mesh_convergence_study.py` **synthesise it** from the geodata if it is missing (width from the outflow boundary line, bed + reach slope from the DEM, trapezoidal banks, roughness from `friction.boundary_*`). To make your own, `hydromate rating -o user-sources/geodata/rating-curve.csv --strickler 38 --slope <S0> --width <b> --side-slope 1 --bed-elevation <z> --q 47`. (Alternatively set `outflow_condition: elevation` with a fixed `prescribed_elevation`, or `free` for a Neumann outflow.)
5. Run the workflow, in order:

```bash
# step 1 - build the complete TELEMAC case into hydromate-case/simulation/
python cases/example-Inn/preprocessing.py
# step 1b - test-run the built case once (confirms it does not crash; ends preprocessing)
python cases/example-Inn/initial_run.py
# step 2 - mesh-convergence study -> styled xlsx report + recommended cell size
python cases/example-Inn/mesh_convergence_study.py
# step 3 - Bayesian calibration with HydroBayesCal (velocity ground truth)
python cases/example-Inn/run_Bayes_cal.py            # --prepare-only writes CSV+config without launching
```

Optional 3D extension - only **after** the 2D hotstart exists (step 1b) and the 2D mesh-convergence study has fixed the horizontal resolution (step 2):

```bash
# write a non-hydrostatic TELEMAC-3D case, hotstarted from the 2D result (reuses the
# 2D horizontal mesh; turbulence + initial layer count inferred; Courant 0.6)
python cases/example-Inn/add3d.py                    # --run also launches telemac3d.py
# then a SEPARATE grid-independence study for the vertical discretization dz: how many
# sigma layers are needed (the 2D study only covered the horizontal cell size)
python cases/example-Inn/vertical_convergence_3d.py
```

   (A one-shot build without the scripts: `hydromate cases/example-Inn/case-config.yml` - or `--check` to validate, `--dry-run` to also run the solver once.)
6. **Step 3 - calibrate** (in the HydroBayesCal clone, with its env):

```bash
cd cases/example-Inn/hydromate-case/calibration-validation
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
