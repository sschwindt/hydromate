# aXqua

**Automated setup, persistent execution and calibration of river models.**

`aXqua` builds a calibration-ready **TELEMAC-2D/3D** case (optionally **+ GAIA** morphodynamics) or an **OpenFOAM `interFoam`** free-surface case from geodata, runs it as a job that outlives the shell that started it, and calibrates it with [HydroBayesCal](https://github.com/Ecohydraulics/hydrobayescal) for surrogate-assisted Bayesian calibration with quantified uncertainty. A **QGIS plugin** drives the whole workflow; everything also works from the command line with QGIS absent.

> Formerly released as `hydromate`. The name changed for legal reasons; the software is the same, and existing cases keep working (see [Migrating from hydromate](#migrating-from-hydromate)).

You provide geodata + hydraulics; `axqua` produces a ready TELEMAC case (geometry `.slf`, `boundaries.cli`, steering `.cas`, friction `.tbl`) plus a `measurements-calibration.csv` and a HydroBayesCal `config_Telemac.py`. An **OpenFOAM `interFoam`** free-surface case can be built from the same configuration and the same converged 2D result.

Simulations run as **persistent jobs** that outlive the shell - or QGIS - that started them, and there is a **QGIS plugin** frontend for the whole workflow. Everything stays fully usable from the command line with QGIS absent.

```bash
axqua submit cases/example-Inn/case-config.yml --kind steady   # -> a job id, immediately
axqua list                                                     # what exists
axqua status <JOB_ID>                                          # where it is
axqua cancel <JOB_ID>                                          # stop it, and everything it started
```

Close the terminal, log out, restart QGIS: the job carries on. See [`docs/jobs.rst`](docs/jobs.rst).

## Migrating from hydromate

The project was renamed for legal reasons. The software is unchanged; only the names are
different, and **nothing you already have needs moving.**

| was | is now |
|---|---|
| `pip install hydromate` | `pip install git+https://github.com/sschwindt/aXqua.git` (not on PyPI yet) |
| `import hydromate` | `import axqua` |
| `hydromate <config>` | `axqua <config>` |
| `hydromate-case/` | `axqua-case/` |
| `hydromate.log` | `axqua.log` |
| `~/.config/hydromate/` | `~/.config/axqua/` |
| `HYDROMATE_JOB_ROOT` and friends | `AXQUA_JOB_ROOT` and friends |
| `<name>.hydromate-prj` | `<name>.axqua-prj` |

The distribution is named `aXqua`; the importable module is plain lowercase `axqua`,
because a mixed-case module name resolves differently on case-insensitive filesystems.

> **Not on PyPI yet.** Install from the repository:
> `pip install git+https://github.com/sschwindt/aXqua.git`. Once it is published,
> `pip install aXqua` will work (PyPI treats `aXqua` and `axqua` as one project).

**The old names are still read** wherever ignoring them would lose your work:

* an existing `hydromate-case/` is used when there is no `axqua-case/` yet, so a case with
  tens of gigabytes of results keeps working untouched. Rename it whenever you like - it
  is a plain move on the same filesystem, so it is instant even when it is large:

  ```bash
  mv cases/<name>/hydromate-case cases/<name>/axqua-case
  ```

  If the config names the folder explicitly (`project.sim_dir`), update that line too.
* `HYDROMATE_*` environment variables still work, with a one-off notice - a shell profile,
  cron entry or systemd unit written before the rename is not something to silently ignore.
* `~/.config/hydromate/profiles.yml` and `~/.local/share/hydromate/` are still read while
  the `axqua` ones do not exist.
* The QGIS plugin still opens a `.hydromate-prj` project, and saves it back as
  `.axqua-prj`. The old file is left in place rather than deleted.

Every one of those is covered by `tests/test_rename_compat.py`, so the migration path is
tested rather than assumed.

Jobs submitted before the rename keep running - they are ordinary system processes and
know nothing about the package name.

## Documentation

Full docs (Sphinx) live in `docs/`, in the order you need them:

| doc | what it covers |
|---|---|
| [`docs/installation.rst`](docs/installation.rst) | environment, install, the TELEMAC link, the QGIS plugin |
| [`docs/preparation.rst`](docs/preparation.rst) | the common preparation workflow: input files, config, ground truth, meshing, structures, building and checking a case |
| [`docs/telemac.rst`](docs/telemac.rst) | the TELEMAC path: initial condition, initial run, numerics, 3D, gain-lose reaches, GAIA |
| [`docs/openfoam.rst`](docs/openfoam.rst) | the OpenFOAM path (seeded by a TELEMAC run), and the rigid-lid shortcut |
| [`docs/outputs.rst`](docs/outputs.rst) | the case folder, what the build writes, and the reports that say whether a run can be believed |
| [`docs/hbc.rst`](docs/hbc.rst) | what aXqua hands to HydroBayesCal |
| [`docs/jobs.rst`](docs/jobs.rst) | submitting, monitoring and cancelling runs that outlive their shell; solver profiles; debugging |
| [`docs/qgis_plugin.rst`](docs/qgis_plugin.rst) | installing and using the QGIS frontend |
| [`docs/architecture.rst`](docs/architecture.rst) | how the model builder, the job runner and the QGIS plugin fit together, and why the seams are where they are |
| [`docs/help.rst`](docs/help.rst) | troubleshooting and tips |

### Building / recompiling the docs

The docs are built with Sphinx. Heavy runtime deps (gmsh, GDAL, geopandas, …) are **mocked** in `docs/conf.py` (`autodoc_mock_imports`), so you can build the docs with just Sphinx + the theme - no need for the full `axqua-env`:

```bash
pip install -r docs/requirements-docs.txt   # one-time: Sphinx + RTD theme
make -C docs html                            # build into docs/_build/html
xdg-open docs/_build/html/index.html         # open it (macOS: `open`)
```

**After editing** any `.rst` under `docs/` (or a docstring - `codedocs.rst` pulls them from `src/` via autodoc), just re-run `make -C docs html`; Sphinx rebuilds only what changed. For a **clean rebuild** (e.g. after moving/renaming pages, or to shake out stale cross-references), wipe the cache first:

```bash
make -C docs clean html                      # remove docs/_build, then rebuild
```

Treat build **warnings** as errors - a `WARNING: ... Inline literal ...` or an undefined `:ref:` means a page won't render as intended. `make -C docs help` lists the other output builders (e.g. `make -C docs linkcheck`). Everything runs locally; nothing leaves your machine.

## Pipeline

```
ROI DEM(s) ─▶ 1. clip to ROI ─▶ 2. gmsh mesh + bathymetry + geometry.slf
                                   │  (per-MATID sizing; FRIC_ID per node)
inflow / outflow / measurements ───┤
                                   ├─▶ 3. boundaries.cli  (inflow 5 5 5 / outflow 4 4 4 | 5 4 4 / wall 2 2 2)
                                   ├─▶ 4. steady2d.cas + friction.tbl  (zonal Manning)
                                   └─▶ 5. measurements-calibration.csv + config_Telemac.py
```

* **Stage 1** (`axqua/dem.py`) - reproject + clip the initial DEM (and optional target DEM) to the ROI boundary; optional **DEM-of-Difference** (`dem_of_difference.enabled`) - the `dem_target - dem_initial` bed-change raster on the ROI-clipped grid, thresholded by a minimum **level of detection** (explicit `min_lod` or the propagated survey uncertainty `t·√(u_i²+u_t²)`; sub-LoD change masked/zeroed).
* **Stage 2** (`axqua/mesh.py`, `selafin.py`) - triangular mesh from boundary + breaklines (anisotropic, flow-aligned in the channel) with per-MATID size fields; DEM interpolated onto nodes; friction zones written as a `FRIC_ID` variable inside the geometry SELAFIN (driven by `geodata.roughness_zones` when set - the polygon `Zone ID` becomes `FRIC_ID` and the table ks becomes the `BOTTOM FRICTION` variable). A **quality report** (`axqua/mesh_quality.py`) is logged for every build: per-region (channel vs floodplain) internal angles, aspect ratio and skewness; the **shortest edge** (critical - it bounds the CFL-limited adaptive time step); and adjacent-cell area jumps (smooth-transition check). The channel's intended elongation is reported but exempt from shape warnings; invalid geometry (zero-area, inverted, duplicate nodes, non-manifold edges) aborts the build.
* **Stage 3** (`axqua/boundary.py`) - classify each contour node against the `boundaries.liquid_boundaries` lines (a line layer whose `Type (inflow/outflow)` field tags every line `inflow` or `outflow`; the lines must coincide with the mesh-zone outer bounds). Inflow nodes get a prescribed-Q boundary (`5 5 5`); outflow nodes get a prescribed-elevation boundary (`5 4 4`) whose water level comes from a fixed `boundaries.prescribed_elevation` (`outflow_condition: elevation`, the **default**) or from the `boundaries.stage_discharge` rating curve at the simulated Q (`outflow_condition: stage_discharge`), or a free/Neumann boundary (`4 4 4`, `outflow_condition: free`); every other outer node is a solid wall (`2 2 2`). If the total inflow- and outflow-node counts differ by more than ~10%, a **stability-risk warning** is logged (rebalance via mesh resolution or line lengths).
* **Stage 4** (`axqua/steering.py`) - friction `.tbl` (one row per friction zone, perturbed by HydroBayesCal) and the `.cas`; GAIA `.cas` when morphodynamics on. Zones come from `friction.zones` (MATID) or, for the Inn case, are derived from the roughness table (`<Zone ID> NIKU <ks> NULL`) so they match the geometry's per-node `FRIC_ID`.
* **Stage 5** (`axqua/calibration.py`, `targets.py`, `ground_truth.py`) - calibration CSV from measurements and the HydroBayesCal `config_Telemac.py`. The recommended way to structure the ground truth is the **calibration-target template**: `axqua targets case-config.yml` generates a user-fillable `user-sources/ground-truth/calibration-target-data.xlsx` whose rows are keyed by **unique IDs** joining point layers (gpkg/shp, any CRS) in `user-sources/geodata/`. Its tabs: `hydraulics` (u_x/u_y/u_z, fluctuations u_x'/u_y'/u_z', auto-computed U_h/U_h'/TKE, water depth, bottom elevation - fillable from SonTek FlowTracker2 exports by the co-located `extract_flowtracker.py` script / `axqua.flowtracker`, keyed by each point's ID; the fluctuation u' is the sample std-dev, not the `VxErr` standard error), `morphodynamics` (d16..d90 grain sizes, fine fraction < 1 mm, plus a `dz` column auto-sampled from the DEM-of-Difference when the case provides a second DEM), and `parameters` (a drop-down over a TELEMAC-2D/3D/GAIA calibration-parameter catalog - friction zones prefilled with their current ks, critical Shields stress, minimum depth, eddy viscosity/diffusivity, secondary currents, ... - with min/max test ranges and range tips; merged into `calibration.parameters`, the template winning on collisions). Reference the filled file under `ground_truth.targets` in the config.

**Logging** - each script/phase writes a compound, timestamped `axqua.log` into its own output folder (the build by `preprocessing.py`/`initial_run.py` -> `axqua-case/simulation/`, the mesh-convergence study -> `axqua-case/mesh-convergence/`), capturing all actions, the elapsed time of each calculation step (`START`/`DONE ... in N.NNs`), and every warning and error. The console mirrors it; pass `-v` for DEBUG.

**Workflow (run in order):** 1. `preprocessing.py` builds the complete TELEMAC case into `axqua-case/simulation/` (final mesh, `.cli`, `.tbl`, `.cas`) at the inflow Q (`boundaries.prescribed_flowrate`) and outflow stage prescription read from **this case's** config - the shared `axqua.prepare_steady_inputs` helper synthesises a constant inflow series / outflow rating only when those inputs are missing; 1b. `initial_run.py` test-runs exactly that case once to confirm it does not crash, then runs a boundary-flux convergence analysis (`axqua.flux_convergence`, reading the solver listing with `axqua.sortie`) that writes four files into `simulation/` (`extracted-fluxes.csv` + `flux-convergence.png`, `convergence-rate.csv` + `convergence-rate.png`), deletes the per-processor `*_p0000N.sortie` copies of a parallel run, and - once the absolute flux imbalance stays below 1e-3 m³/s over 10 consecutive printouts (or in the 10-printout mean on a noisy steady state) - generates `hotstart2d.cas`, a continuation of `r2d.slf` capped at that steady time with the constant Q/H prescriptions kept. Balanced fluxes say nothing about *where* the water is, so it also writes `wetting-report.csv` and `outlet-profile.csv` (`axqua.wetting`): the first splits the wetted area into actively flowing water, stagnant film and isolated puddles, says how much of each the pre-wet seed put there, and whether the film is still draining or has plateaued (water perched above the converged surface can never leave a model with no infiltration or evaporation); the second profiles the free surface approaching the outflow and reports whether the prescribed stage is backing water up, pulling it down, or neutral. This ends preprocessing; 2. `mesh_convergence_study.py` runs the grid-independence study; 3. `run_Bayes_cal.py` calibrates the built case with HydroBayesCal against the FlowTracker velocity ground truth (the velocity measured at 0.6·h ≈ depth-averaged, compared to `SCALAR VELOCITY`), writing the calibration artifacts into `axqua-case/calibration-validation/`.

**Initial-run numerics** - the 2D case uses the finite-element kernel with an auto-selected turbulence model (k-epsilon / Smagorinski / Spalart-Allmaras by mesh resolution) and a CFL-adaptive time step. The steering ships **compute-stable defaults** so the wetting/drying steady march does not explode: target Courant 0.30 (`time_step: 0.25` is only the start step), `IMPLICITATION FOR DEPTH/VELOCITY 0.80`, `FREE SURFACE GRADIENT COMPATIBILITY 0.9`, `DISCRETIZATIONS IN SPACE 11;11`, `H CLIPPING : NO` and a raised k-epsilon solve budget; the graphic printout is `'U,V,S,B,H,M,Q,F'` plus `K,E` (TKE + dissipation) for k-epsilon. It is a **dry start**: only a thin water plug at the inflow line is seeded so the prescribed-Q boundary can establish (a fully dry bed makes TELEMAC's `DEBIMP` abort), and the rest of the domain wets from the inflow - set `initialization.prewet_depth` to hotstart the whole channel instead. With the variable time step the run is bounded by `DURATION` (`hydrodynamics.duration`, seconds), and **convergence is judged by the boundary-flux balance**, not by the unreliable steady-state auto-stop (off by default). All of this is emitted by `preprocessing.py` (via `pipeline.run`); the mesh-convergence study reuses the same numerics per mesh.

**Gain-lose reaches** (`gain_lose` config block) - a reach that loses flow into a porous body (a gravel bar, an alluvial patch) and regains it downstream. A 2D model has no subsurface, so the underflow becomes an internal withdrawal where water infiltrates plus an injection where it resurfaces, generated into a `USER_RAIN` routine that is **depth-limited** (it can never dry a cell) and **mass-exact** (what it takes is returned in the same step, so no net sink appears in the boundary budget). Point `zone` at the porous body and, by default, **nothing else has to be drawn**: the body's water table - the phreatic surface joining the two channel levels it exchanges with - decides where the reach loses (wet, free surface above the table) and where it gains (table above the bed), re-evaluated each step so the faces move with the stage. `faces: lines` instead pins the exchange to `int-*` lines you draw, for a surveyed seepage face. The magnitude is either a measured `discharge` or, by default, the conductivity `kf` via Green-Ampt - riverbed `kf` spans orders of magnitude (clean gravel 1e-2..1e-1, colmated 1e-4..1e-3, silted 1e-7..1e-5 m/s; see Calver 2001, [doi:10.1111/j.1745-6584.2001.tb02343.x](https://ngwa.onlinelibrary.wiley.com/doi/10.1111/j.1745-6584.2001.tb02343.x)), so it is exposed to HydroBayesCal as a calibration parameter rather than assumed. The water table additionally seeds closed depressions *on* the bar, which no surface flow can ever reach.

**Optional 3D extension (after the 2D path):** the 2D simulation is the foundation - a 3D run is only built *once the 2D run has produced its hotstart result* (`initial_run.py` → `r2d.slf`) and *after the 2D mesh-convergence study has settled the horizontal resolution*. Then `add3d.py` writes three TELEMAC-3D cases hotstarted from the 2D result (a hydrostatic steady flux-convergence check, the non-hydrostatic steady run with in-file Q/H, and a non-hydrostatic unsteady case driven by the same hydrograph forcing files as `unsteady2d.cas`), and - because the vertical discretization `dz` (the number of sigma layers) is a **new** discretization choice that the 2D study never touched - `vertical_convergence_3d.py` runs a **second, separate grid-independence study over the number of vertical layers**, the 3D analogue of step 2.

**Mesh-convergence study** (`axqua/convergence.py`, step 2) - a grid-independence check: the same steady simulation at a constant discharge on five meshes (the configured baseline plus two coarser at +40%/+20% cell size and two finer at -20%/-40%, each with TELEMAC's variable time step for its own CFL-admissible dt), sampling water depth and scalar velocity at the ground-truth probe points and reporting the relative change between successive refinements, an observed order of convergence and a Grid Convergence Index against a tolerance (default 2%). It writes a **styled `.xlsx` report** to `axqua-case/postprocessing/` with a **recommended cell size** balancing grid independence against compute time. Results are read back with a small SELAFIN reader (`selafin.read_slf`).

**3D extension & vertical-layer convergence** (`axqua/threed.py`, `vertical_convergence.py`) - optional, and strictly *after* the 2D path. A 3D run needs the converged 2D result as its hotstart, so it follows `initial_run.py`; and you only build it on a horizontal mesh whose resolution the 2D mesh-convergence study (step 2) has already vouched for - the 3D case reuses that same horizontal mesh. `add3d.py` writes three steering files (`threed.build_3d_cases`): `hotstart3d_hydrostatic.cas` (`NON-HYDROSTATIC VERSION : NO`, constant Q/H, ~30k fixed steps with a short listing period - the steady boundary-flux convergence check), `hotstart3d_hydrodyn.cas` (non-hydrostatic steady, in-file prescribed Q and H), and `unsteady3d.cas` (non-hydrostatic, hydrograph Q(t) + outflow SL(t) via the same liquid-boundaries file as `unsteady2d.cas`; needs a varying `boundaries.inflow`). All use sigma layers, with the turbulence model and an initial layer count inferred from the 2D result and the time step sized for Courant 0.6; `--run [hydrostatic|hydrodyn|unsteady]` launches one of them. The vertical discretization `dz` is then its **own** discretization question - varying the horizontal cell size in step 2 tells you nothing about how many vertical layers you need - so `vertical_convergence_3d.py` re-runs the 3D case over a ladder of vertical-layer counts on the same horizontal mesh and reports the relative change / observed order / GCI against `dz`, recommending the **fewest grid-independent number of layers**. Outputs go to `axqua-case/postprocessing/vertical-convergence/`.

## Install

### Requirements

## Installing conda/mamba on Debian Linux

Debian does not provide `conda` or `mamba` by default. To install **Miniforge**, which provides both `conda` and `mamba` via conda-forge use:

```bash
# Install basic download dependencies
sudo apt update
sudo apt install -y wget ca-certificates bzip2

# Download the latest Miniforge installer for 64-bit Linux
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh

# Install Miniforge into ~/miniforge3
bash Miniforge3-Linux-x86_64.sh -b -p "$HOME/miniforge3"

# Initialize conda/mamba for bash
"$HOME/miniforge3/bin/conda" init bash

# Reload the shell configuration
source ~/.bashrc
```

## Install environment

The case-build pipeline runs in its **own** environment (`axqua-env`); it does *not* import TELEMAC's Python. Instead it **sources** the TELEMAC `pysource.*.sh` (set in the config) whenever the solver or SELAFIN tooling is needed.

```bash
mamba env create -f environment.yml
mamba activate axqua-env
pip install -e .
```

### Configuration editor (GUI)

Instead of hand-editing the YAML you can fill the configuration in as a browser form (Streamlit). Install the `gui` extra and launch it:

```bash
pip install -e ".[gui]"
axqua-gui                 # opens a local app in your browser; nothing leaves your machine
# axqua-gui --server.port 8600   # extra args are forwarded to Streamlit
```

The form mirrors every config section (Project, TELEMAC, Inputs, Mesh, Friction, Hydrodynamics, Morphodynamics, Calibration) plus a **Workflow** tab summarising the steps; friction zones, calibration parameters, ground-truth sources and sediment classes are edited as tables. You can load an existing YAML, preview/download the generated YAML, save it, and run **Validate** (`--check`) or **Build** (the case build = workflow step 1) directly - the later steps (test run, mesh convergence, calibration, the 3D extension) are run from the per-case scripts.

## The QGIS plugin

`qgis_plugin/axqua/` is a QGIS 3.44+/4.x plugin that drives all of the above from a
dock: choose a case, submit a run, watch it, and load the results as styled layers.

**It never imports `axqua`.** QGIS ships its own Python and aXqua needs gmsh,
rasterio, geopandas and a solver environment, so the plugin talks to the `axqua`
command-line tool as a subprocess and reads the files it writes. Either side can be
reinstalled without touching the other - and, because the plugin does not own the solver
process, closing QGIS does not stop your simulation.

The tabs between *Setup* and *Jobs* are **generated from what aXqua reports the case
can do** (`axqua case-status --json`), so a capability added to aXqua appears in
the plugin with no plugin change.

For development, link it into your QGIS profile:

```bash
ln -s "$PWD/qgis_plugin/axqua" \
      ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/axqua
```

Then enable *aXqua* in **Plugins > Manage and Install Plugins**. See
[`docs/qgis_plugin.rst`](docs/qgis_plugin.rst) and
[`qgis_plugin/axqua/README.md`](qgis_plugin/axqua/README.md).

The plugin is GPL-2.0-or-later, because it links PyQGIS; `axqua` itself stays
BSD-3-Clause and remains usable on its own.

## Case layout

Each case lives in its own folder under `cases/<case-name>/`. To start a new case, copy the **`cases/case-template/`** scaffold (config + scripts + `USAGE.info`, no data) to `cases/<your-case>/`, drop your data into `user-sources/`, and edit `case-config.yml`. The worked, filled-in example is `cases/example-Inn/`:

```
cases/example-Inn/
  case-config.yml      # the case configuration (tracked)
  preprocessing.py            # step 1: build the full TELEMAC case into simulation/ (tracked)
  initial_run.py              # step 1b: test-run the built case (tracked)
  mesh_convergence_study.py   # step 2: grid-independence study + xlsx report (tracked)
  run_Bayes_cal.py            # step 3: HydroBayesCal calibration (velocity ground truth) (tracked)
  add3d.py                    # optional (after 2D): the three 3D cases, hotstart from 2D (tracked)
  vertical_convergence_3d.py  # optional (after 3D): vertical-layer (dz) convergence study (tracked)
  openfoam_preprocessing.py   # optional (after 1b): build the OpenFOAM interFoam case (tracked)
  openfoam_run.py             # optional: two-stage interFoam run + discharge report (tracked)
  user-sources/        # your large source data - DEMs, GeoPackages, ground truth (gitignored)
  axqua-case/       # produced artifacts, by workflow phase (gitignored):
    preprocessing/         # DEM clips, meshes, ground-truth table + its axqua.log
    simulation/            # the TELEMAC case (geometry.slf, .cli, .cas, .tbl, results) + its axqua.log
    postprocessing/        # general post-processing
    mesh-convergence/      # convergence study: mesh-convergence.xlsx/.txt, per-mesh runs, log
    vertical-convergence/  # 3D vertical-layer (dz) study: vertical-convergence.xlsx/.txt, per-level runs
    calibration-validation/  # HydroBayesCal artifacts (measurements-calibration.csv, config_Telemac.py)
    openfoam/              # optional OpenFOAM case: 0/ constant/polyMesh/ system/ + discharge-convergence.csv/.png
```

Config paths resolve relative to `case-config.yml`, so `user-sources/...` points at your data and the build writes into `axqua-case/`. Each script logs into its own output folder (`preprocessing/`, `simulation/`, `postprocessing/`). Only the config, scripts and docs are version-controlled; `user-sources/` and `axqua-case/` stay out of git (they run to gigabytes - see the 20 MB CI guard in `.github/workflows/`).

## Use

1. Edit `cases/example-Inn/case-config.yml` - point `telemac.pysource` at your TELEMAC env, set the input paths, mesh sizes, friction zones, and calibration parameters/ranges.
2. Provide a **ROI boundary polygon** (`geodata.boundary`): a closed polygon (or closed polyline) delineating the maximum wetted extent, in EPSG:25832.
3. Provide the **liquid boundaries** (`boundaries.liquid_boundaries`): a line layer in EPSG:25832 whose `Type (inflow/outflow)` field tags each line `inflow` or `outflow` (several of each are allowed). Draw them **exactly along the mesh-zone outer bounds** so contour nodes land on them. Keep the inflow and outflow lines a similar length relative to the mesh resolution so they carry comparable node counts (within ~10%), or the build logs a stability-risk warning.
4. **Outflow water level** (`boundaries.outflow_condition`, default `elevation`): by default the downstream water level is a fixed `boundaries.prescribed_elevation` (m a.s.l.). Alternatively set `outflow_condition: stage_discharge` with a `boundaries.stage_discharge` `Q,WSE` CSV that sets the water level at the simulated discharge (one Q-h pair at the steady Q is enough); you don't have to make one - `preprocessing.py`/`mesh_convergence_study.py` **synthesise it** from the geodata if it is missing (width from the outflow boundary line, bed + reach slope from the DEM, trapezoidal banks, roughness from `friction.boundary_*`), or make your own with `axqua rating -o user-sources/geodata/rating-curve.csv --strickler 38 --slope <S0> --width <b> --side-slope 1 --bed-elevation <z> --q 47`. Or set `free` for a Neumann outflow (nothing prescribed).
5. **Ground truth + calibration ranges** (recommended): generate the calibration-target template, fill it in, and reference it in the config:

```bash
axqua targets cases/example-Inn/case-config.yml   # writes calibration-target-data.xlsx + extract_flowtracker.py
```

   Enter each measurement with a **unique ID** matching the `ID` field of a point layer in `user-sources/geodata/` (declared as `ground_truth.targets.hydraulics_positions` / `sediment_positions`; any CRS - reprojected on ingest), or with explicit x/y. The `hydraulics` tab takes velocities, fluctuations, depth and bed elevation (U_h, U_h' and TKE compute themselves); the `morphodynamics` tab takes d16..d90 + fine fraction, and its `dz` column is auto-filled from the DEM-of-Difference; the `parameters` tab picks calibration parameters from a drop-down (with range tips) and feeds `calibration.parameters`. To fill the `hydraulics` tab straight from SonTek FlowTracker2 exports, run the `extract_flowtracker.py` script dropped next to the template (each point keyed by its ID):

```bash
mamba run -n axqua-env python cases/example-Inn/user-sources/ground-truth/extract_flowtracker.py FlowTracker2-day1.xlsx FlowTracker2-day2.xlsx
```

6. Run the workflow, in order:

```bash
# step 1 - build the complete TELEMAC case into axqua-case/simulation/
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
python cases/example-Inn/add3d.py                    # --run [hydrostatic|hydrodyn|unsteady] also launches telemac3d.py
# then a SEPARATE grid-independence study for the vertical discretization dz: how many
# sigma layers are needed (the 2D study only covered the horizontal cell size)
python cases/example-Inn/vertical_convergence_3d.py
```

Optional **OpenFOAM free-surface (VOF)** extension - also after step 1b, and independent
of the TELEMAC-3D one. Use it when the vertical structure matters *and* the water
surface has a gradient, which rules out `simpleFoam`. Point `openfoam.bashrc` at your
OpenFOAM `etc/bashrc` and add an `openfoam:` block (see `cases/case-template/case-config.yml`):

```bash
# how many cells would the current settings give? (no build)
axqua openfoam cases/example-Inn/case-config.yml --check
# build the case: a terrain-following ALL-HEXAHEDRAL mesh written straight to
# constant/polyMesh (no snappyHexMesh), fields seeded from the converged r2d.slf
python cases/example-Inn/openfoam_preprocessing.py
# checkMesh -> decomposePar -> interFoam (spin-up, then production) -> reconstructPar
# -> inlet/outlet water-discharge convergence report
python cases/example-Inn/openfoam_run.py
```

The air phase is the usual reason these runs fail, so three things address it directly:
the **lid follows the 2D free surface** at a fixed `freeboard`, so most air cells never
exist; **semi-implicit MULES** (`MULESCorr`) lets the Courant target run at ~0.9 instead
of the tutorials' 0.2; and a **`limitVelocity` constraint** caps `|U|` at several times
the reach's own water speed, which water never reaches but a runaway air jet does. Watch
`Co` and `dt` on the progress bar: a healthy run holds `dt` near the Courant target.

One caveat the build reports for you: OpenFOAM's rough wall function needs the first cell
centre *above* the roughness crests, and on a gravel bed `ks` can be a large fraction of
the depth. Where it is not satisfied the solver does not fail - it clamps and carries on -
so read such a result as bulk flow, not as a resolved bed boundary layer.

   (A one-shot build without the scripts: `axqua cases/example-Inn/case-config.yml` - or `--check` to validate, `--dry-run` to also run the solver once.)
7. **Step 3 - calibrate** (in the HydroBayesCal clone, with its env):

```bash
cd cases/example-Inn/axqua-case/calibration-validation
python /home/schwindt/github/hydrobayescal/bal_telemac.py --config config_Telemac.py
```

## One case, two solvers

aXqua describes a reach **once** - ROI, liquid boundaries, mesh zones and
centerline, roughness zones, structures, discharge, ground truth - and builds either
or both simulation backends from it. Each solver adds only the knobs that are
genuinely its own.

|  | **TELEMAC-2D/3D (+GAIA)** | **OpenFOAM `interFoam`** |
|---|---|---|
| answers | depth-averaged flow, morphodynamics, calibration | the vertical structure of the flow |
| mesh | anisotropic flow-aligned triangles → `geometry.slf` | terrain-following all-hex lattice → `constant/polyMesh` |
| free surface | a state variable of the shallow-water equations | a resolved two-phase (VOF) interface |
| cost | hours | hours to days (the build prints a cost report) |
| run it | always - it is also the 3D run's hotstart | after the 2D run has converged |

```bash
# shared: describe the reach, then see what is set up
axqua status cases/example-Inn/case-config.yml

# TELEMAC
python cases/example-Inn/preprocessing.py            # build
python cases/example-Inn/initial_run.py              # test-run + hotstart convergence
python cases/example-Inn/mesh_convergence_study.py   # grid independence
python cases/example-Inn/run_Bayes_cal.py            # Bayesian calibration

# OpenFOAM (after the 2D run has converged)
axqua openfoam cases/example-Inn/case-config.yml --check   # cell count, no build
python cases/example-Inn/openfoam_preprocessing.py             # build
python cases/example-Inn/openfoam_run.py                       # spin-up, run, report
```

## Structures: dams, weirs, walls and buildings

A structure is an **ordinary QGIS vector layer**, not a triangulated surface. STL is a
`snappyHexMesh` requirement; aXqua writes its own mesh, so a structure only has to
say **where its footprint is** and **how high it stands**. No CAD step, nothing QGIS
cannot author.

Draw it either way, and say how high it stands in one of two ways:

| you draw | you get |
|---|---|
| a **polygon** | the footprint directly (building, dam body, pier) |
| a **line** + `Width (m)` | buffered to a footprint - trace the crest, say how thick |
| `Crest (m)` | a **level** crest: dam, weir, floodwall |
| `Height (m)` | a crest that **follows the terrain**: embankment, levee |

Two modes, chosen from the `Type` text (or an explicit `Mode` column):

| mode | what it is | what the mesh does |
|---|---|---|
| `overflow` | dam, weir, levee, block ramp - water passes over | the **bed is raised to the crest**; identical in both solvers |
| `solid` | wall, floodwall, building, pier - never overtopped | OpenFOAM **removes the footprint** (no-slip walls bed to lid); TELEMAC raises the bed to crest + `solid_freeboard_2d`, the standard 2D practice |

Anything unrecognised is `overflow` - raising the bed keeps the domain connected and
lets water pass, whereas wrongly blanking a footprint would silently wall off part of
the reach. A solid structure that cuts the domain in two is reported, not applied
silently.

```yaml
geodata:
  structures: user-sources/geodata/structures.gpkg
structures:
  crest_field: "Crest (m)"      # or height_field: "Height (m)"
  default_width: 1.0            # for lines with no Width
  solid_freeboard_2d: 2.0       # TELEMAC only
```

## What a case can do

Every case carries one marker file per solver at its top level, refreshed by any build
and by `axqua status`:

```bash
axqua status cases/example-Inn/case-config.yml          # summary + refresh markers
axqua status cases/example-Inn/case-config.yml --full   # the whole table
axqua status cases/example-Inn/case-config.yml --check-env   # also probe the solvers
```

```
cases/example-Inn/
  MODEL=TELEMAC_ENABLED     # the name says whether the CASE declares this solver
  MODEL=OPENFOAM_DISABLED   # (so it means the same on any machine)
```

The body reports each capability on three axes, which answer three different questions:

| axis | question | example |
|---|---|---|
| `implemented` | does aXqua support this **for this solver**? | `yes` / `no` (not yet) / `n/a` (never - OpenFOAM has no depth-averaged mode) |
| `configured` | does **this case** ask for it? | a varying inflow series implies `unsteady2d` |
| `built` / `run` | do the **artifacts** exist? | `steady2d.cas` written, `r2d.slf` produced |

The files are generated, so they are gitignored - they describe the *currently available*
setup, which is local state like `axqua-case/`. `cases/case-template/` keeps a
committed example.

## Configuration reference

See `cases/example-Inn/case-config.yml` for a fully commented example. Key sections: `project` (name, CRS, output dirs), `telemac` (pysource, solver, processors), `geodata` (DEMs, ROI boundary, breaklines, region/MATID points, mesh/roughness zones, centerline), `boundaries` (liquid boundaries, prescribed inflow Q, outflow condition + prescribed elevation / stage-discharge, optional inflow series), `initialization` (dry-start / pre-wetting), `mesh`, `friction` (zones ↔ MATID), `hydrodynamics` (numerics), optional `morphodynamics` (GAIA), `ground_truth` (the calibration-target template under `targets`, a user-authored tidy `measurements` table, or raw `sources`), and `calibration`.

### Calibration parameter naming (HydroBayesCal convention)

| Prefix                              | Target                              |
|-------------------------------------|-------------------------------------|
| `zone<MATID>`                       | bed-friction coefficient of a zone  |
| `gaiaCLASSES SHIELDS PARAMETERS <n>`| GAIA critical Shields, sediment `n` |
| `vg_zone<MATID>-<p>`                | vegetation friction parameter       |
| any literal TELEMAC keyword         | written straight into the `.cas`    |

## Coordinate system

All inputs/outputs are **EPSG:25832 (ETRS89 / UTM 32N)**, metres - see `CLAUDE.md`. Inputs in another CRS are reprojected on ingest.

## Cases (Backup)

`cases/*/user-sources/` (raw DEMs, orthos, gauge/FlowTracker/GSD data) and `cases/*/axqua-case/` (produced TELEMAC artifacts) are gitignored and local-only - some of that data (field campaign exports, DGPS shapefiles, gauge CSVs) is irreplaceable raw field data, not just reproducible output. `scripts/backup_cases_to_drive.sh` mirrors both folders for every case to Google Drive via [rclone](https://rclone.org/):

```bash
sudo apt install rclone
rclone config                              # one-time: create a remote named "gdrive" (OAuth in browser)
rclone lsd gdrive:                         # sanity check

./scripts/backup_cases_to_drive.sh         # dry run: prints what would change
./scripts/backup_cases_to_drive.sh --run   # actually syncs (uploads AND deletes remote-only
                                            #   files, mirroring local - review the dry run first)
```

Remote layout mirrors local: `gdrive:aXqua-Cases/<case-name>/user-sources/` and `.../axqua-case/`. Restore a case with `rclone copy gdrive:aXqua-Cases/<case-name> cases/<case-name>`. It's manual/on-demand (no cron) - rerun it yourself after a case produces new heavy data. Each run logs to `scripts/backup-logs/` (gitignored).

## Status

v1 covers the **2D hydraulic** path end to end (friction-zone calibration against water depth / velocity). The **DEM-of-Difference** with a level of detection is implemented in pre-processing (`dem_of_difference` config block); GAIA morphodynamics and turning the DoD into per-point topographic-change calibration targets are wired as extension points (`morphodynamics` config block, GAIA `.cas` writer) and built out next.
