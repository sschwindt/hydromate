# Inn TELEMAC setup workflow (`hydromate`)

Automated setup of a calibration-ready **TELEMAC-2D** (optionally **+ GAIA** morphodynamics) case for a reach of the **Inn river (Bavaria)**, wired into [HydroBayesCal](https://github.com/Ecohydraulics/hydrobayescal) for surrogate-assisted Bayesian calibration with quantified uncertainty.

You provide geodata + hydraulics; `hydromate` produces a ready TELEMAC case (geometry `.slf`, `boundaries.cli`, steering `.cas`, friction `.tbl`) plus a `measurements-calibration.csv` and a HydroBayesCal `config_Telemac.py`.

## Pipeline

```
ROI DEM(s) ─▶ 1. clip to ROI ─▶ 2. gmsh mesh + bathymetry + geometry.slf
                                   │  (per-MATID sizing; FRIC_ID per node)
inflow / outflow / measurements ───┤
                                   ├─▶ 3. boundaries.cli  (inflow 5 5 5 / outflow 5 4 4)
                                   ├─▶ 4. steady2d.cas + friction.tbl  (zonal Manning)
                                   └─▶ 5. measurements-calibration.csv + config_Telemac.py
```

* **Stage 1** (`hydromate/dem.py`) — reproject + clip the initial DEM (and optional target DEM) to the ROI boundary; optional DEM-of-Difference for morphodynamics.
* **Stage 2** (`hydromate/mesh.py`, `selafin.py`) — triangular mesh from boundary + breaklines with per-MATID size fields; DEM interpolated onto nodes; friction zones written as a `FRIC_ID` variable inside the geometry SELAFIN.
* **Stage 3** (`hydromate/boundary.py`) — classify contour nodes against the inflow/outflow lines; write `.cli` with the codes the solver expects.
* **Stage 4** (`hydromate/steering.py`) — friction `.tbl` (one row per MATID, perturbed by HydroBayesCal) and the `.cas`; GAIA `.cas` when morphodynamics on.
* **Stage 5** (`hydromate/calibration.py`) — calibration CSV from measurements and the HydroBayesCal `config_Telemac.py`.

## Install

The case-build pipeline runs in its **own** environment (`hydromate-env`); it does *not* import TELEMAC's Python. Instead it **sources** the TELEMAC `pysource.*.sh` (set in the config) whenever the solver or SELAFIN tooling is needed.

```bash
mamba env create -f environment.yml
mamba activate hydromate-env
pip install -e .
```

## Use

1. Edit `config/inn.yml` — point `telemac.pysource` at your TELEMAC env, set the input paths, mesh sizes, friction zones, and calibration parameters/ranges.
2. Provide a **ROI boundary polygon** (`inputs.boundary`): a closed polygon (or closed polyline) delineating the maximum wetted extent, in EPSG:25832.
3. Build the case:

```bash
hydromate config/inn.yml            # build everything
hydromate config/inn.yml --check    # validate config only
hydromate config/inn.yml --dry-run  # build, then run the solver once to validate
```

4. Calibrate (in the HydroBayesCal clone, with its env):

```bash
cd case/simulation
python /home/schwindt/github/hydrobayescal/bal_telemac.py --config config_Telemac.py
```

## Configuration reference

See `config/inn.yml` for a fully commented example. Key sections: `project` (name, CRS, output dirs), `telemac` (pysource, solver, processors), `inputs` (DEMs, boundary, breaklines, region/MATID points, liquid boundaries, inflow, optional stage-discharge + measurements), `mesh`, `friction` (zones ↔ MATID), `hydrodynamics`, optional `morphodynamics` (GAIA), and `calibration`.

### Calibration parameter naming (HydroBayesCal convention)

| Prefix                              | Target                              |
|-------------------------------------|-------------------------------------|
| `zone<MATID>`                       | bed-friction coefficient of a zone  |
| `gaiaCLASSES SHIELDS PARAMETERS <n>`| GAIA critical Shields, sediment `n` |
| `vg_zone<MATID>-<p>`                | vegetation friction parameter       |
| any literal TELEMAC keyword         | written straight into the `.cas`    |

## Coordinate system

All inputs/outputs are **EPSG:25832 (ETRS89 / UTM 32N)**, metres — see `CLAUDE.md`. Inputs in another CRS are reprojected on ingest.

## Status

v1 covers the **2D hydraulic** path end to end (friction-zone calibration against water depth / velocity). GAIA morphodynamics and the DEM-of-Difference topographic-change calibration are wired as extension points (`morphodynamics` config block, `dem.dem_of_difference`, GAIA `.cas` writer) and built out next.
