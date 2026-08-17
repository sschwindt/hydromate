# Case: inn-KB15-2025-hydro

TELEMAC-2D hydraulic model of the Inn river subreach **KB15** (Bavaria), built
from the 2025 UAS survey and calibrated with HydroBayesCal against FlowTracker2
field campaigns at several steady discharges (multi-flow Bayesian calibration
of the channel roughness).

## Structure and workflow

Standard aXqua case layout (see the repository `README.md` and
`CLAUDE.md`); everything is driven by `case-config.yml` (all paths relative to
this folder). Ordered steps:

1. `preprocessing.py` - full case build into `axqua-case/simulation/`
   (mesh `geometry.slf`, `boundaries.cli`, `friction.tbl`, `steady2d.cas`).
2. `initial_run.py` - steady test run + boundary-flux convergence check.
3. `mesh_convergence_study.py` - horizontal grid independence.
4. `prepare_corrected_targets.py` - **case-specific**: compiles the corrected
   calibration-target CSVs into `axqua-case/preprocessing/` (see "Data
   particularities" - run this before any calibration re-run).
5. `run_Bayes_cal_multiflow.py [--smoke|--run|--resume]` - multi-discharge
   Bayesian calibration (one shared channel `ks` against all campaigns
   jointly). `run_Bayes_cal.py` is the single-flow variant.
6. Optional: `add3d.py` / `vertical_convergence_3d.py` (3D extension),
   `unsteady_run.py` (hydrograph-driven run).

Inputs live in `user-sources/` (gitignored), produced artifacts in
`axqua-case/` (gitignored), split by phase. Calibration artifacts land in
`axqua-case/calibration-validation/multiflow/` (per-flow trees
`flow-<name>/`, combined surrogate + posterior under
`auto-saved-results-HydroBayesCal/`).

## Field campaigns (ground truth)

| campaign | Q (m3/s) | points | kind | role |
|---|---|---|---|---|
| Sept 2025 | 47.3 | 30 verticals, reach-spanning (2 pools) | `adapter` | main velocity + water-level target |
| Nov 2025 | 48.45 | 22 verticals, one taped cross-section | `transect` | secondary target (down-weighted) |
| March 2026 | 45.8 | 5 near-bank margin verticals | `inline` | **excluded (comparability, not discharge)** - the old "Q=168, model ~5x too fast" reason was a discharge mislabel; at the true 45.8 m3/s the field magnitudes match the model to order, but these few margin points sit in partially-wet cells and add no discharge diversity (45.8 ~= 47.3 ~= 48.45). Wadeable velocity surveys are safety-capped at low flow, so high flow must enter via water-level / flood-extent targets. |

Values: `user-sources/ground-truth/hydraulics/FT_TKE_Summary*.xlsx`;
positions: `user-sources/geodata/flowtracker2/` and `TKE_KB15_Nov25.gpkg`.

## Data particularities (read before touching the ground truth)

### 1. DGPS pole-height offset (Sept 2025)

The raw DGPS layer `dgps-flowtracker-kb15-sept25.gpkg` stores the **GNSS
antenna elevation** in `z`, not the bed: the rover pole was set to **2.26 m**
in the upstream pool (verticals 1501-1511), **2.70 m** in the downstream pool
(1513-1530) and **2.51 m** for the transitional vertical 1512. The pole
heights were recovered from the flat-water-surface condition
(`z + WaterDepth - WSE = pole`, constant to +/-2 cm within each pool) and are
consistent with standard pole lock positions. Use
**`dgps-flowtracker-kb15-sept25-zcorrected.gpkg`** (bed `z = z_raw - pole`;
carries `z_raw_antenna`, `pole_height`, `wse_implied`). Never join targets to
the raw layer's `z`.

### 2. Wetted-channel bathymetry bias of the 2025 DEM

`DEM-2025-20cm.tif` is refraction-corrected bathymetric LiDAR (confirmed),
yet in the wetted channel its bed is **~0.27 m (pool 1) to 0.33 m (pool 2)
too high on average** against the pole-corrected DGPS beds, and the offset
**grows with water depth** (pool 1: +0.69 m per m of depth, r = 0.80,
p = 0.003; pool 2: +0.23, r = 0.39). A depth-proportional bias cannot come
from a datum/pole error (that would be constant); the mechanism is laser
attenuation in the glacially turbid Inn water column - the bathymetric LiDAR
resolves the shallow wetted areas well but loses or biases the bottom returns
in the deeper pools. Consequences:

* modelled water **levels** are excellent (model WSE matches the corrected
  DGPS water surface within ~1 cm in both pools at Q~47 m3/s);
* modelled **depths** are biased low and point **velocities** biased high
  (continuity through the artificially reduced section) - raw depth/velocity
  measurements are NOT directly comparable to the model at these points.

The calibration therefore uses **bathymetry-corrected depth targets**:
`WATER DEPTH_DATA := WSE_measured - bed_model(x, y)` (the depth the model
should show given its own bed if its water level is right - mathematically a
water-level calibration), and adds a **structural discrepancy term** to the
velocity errors (0.10 m/s Sept, 0.20 m/s Nov, ~`U * dbed / h`). All of this is
implemented and documented in `prepare_corrected_targets.py`. The proper
long-term fix is fusing the DGPS bed points (plus echo-sounding or denser
wading survey in the deeper pools) into the DEM's wetted channel, followed by
a rebuild.

### 3. Multi-depth ADV verticals (Sept 2025)

20 of the 30 Sept verticals were measured at **three depths**
(~0.3h / 0.6h / 0.9h; `profiles` sheet of `FT_TKE_Summary.xlsx`, one row per
measurement, same x-y per vertical). They are used two ways:

* **Calibration targets:** the velocity target for those verticals is the
  USGS three-point depth average `(u02 + 2*u06 + u08) / 4` instead of the
  single 0.6h proxy (mean shift -0.015 m/s, up to 0.08 m/s). Implemented in
  `prepare_corrected_targets.py`; single-point verticals keep the 0.6h value.
* **Vertical-profile evidence:** log-law fits `u(z') = (u*/kappa) ln(z'/z0)`
  per vertical (see `axqua-case/preprocessing/kb15-loglaw-profiles.csv`)
  give `ks = 30 z0` with median **0.089 m** (IQR 0.05-0.41 m, very noisy at
  these low wadeable velocities) - i.e. the profiles support a roughness well
  inside the prior `[0.05, 0.45]` m and clearly below its upper bound. They
  also confirm near-logarithmic profiles, which justifies (a) the 0.6h point
  as a depth-average proxy for single-point verticals in 2D and (b) the
  logarithmic `VELOCITY VERTICAL PROFILES` prescription used by the 3D
  extension (`add3d.py`).

### 4. Nov-25 transect internal inconsistency (open QA item)

Within the single 10 m Nov transect the implied water surface `z + depth`
spreads by 0.34 m - physically impossible. Its vertical 1 agrees with the
corrected Sept pool-1 water surface; the transect's water level is therefore
anchored to that surface (376.32 m at Q~48 m3/s) and its depth-target error
widened to 0.10 m. The raw `z`/depth columns of the other verticals need
field-book QA.

## Calibration setup and history

* Parameter: channel Nikuradse `ks` (`zone1` in `friction.tbl`), prior
  `[0.05, 0.45]` m (`d50 .. 3 d90` from the bed GSD). Floodplain (`zone2`)
  fixed at 0.5 m.
* Targets: `WATER DEPTH` (bathymetry-corrected water level) + `SCALAR
  VELOCITY` (profile-averaged, discrepancy-widened) - 52 points, 104 values.
* Design: 8 initial grid runs + up to 7 Bayesian-Active-Learning iterations,
  each collocation point = one TELEMAC run per flow (~50 min on 16 cores);
  `--resume` reuses the per-flow initial designs
  (`flow-<name>/.../restart_data/`).
* **History (2D calibrations, `multiflow-*` archives):**
  1. *ks-only, velocity-only, uncorrected data* (Jul 2026): posterior pinned
     at the prior upper bound (`ks -> 0.45`). Diagnostics traced this to the
     DEM bathymetry bias (item 2), not roughness; an outflow-stage sensitivity
     run (+0.30 m, `steady2d-q47-3-stagetest.cas`) ruled out the downstream
     boundary (both rating stages sit below the natural outfall level; steep
     controls isolate the measurement subreach).
  2. *ks-only, corrected water-level + velocity targets* (archived
     `multiflow-2026-07-15-ks-only/`): interior posterior **ks = 0.411 m**,
     90% CI [0.348, 0.446]; RE ~1.27 nats, 906 posterior samples. Water levels
     fit to ~1 cm; velocity still over-predicted (+0.19 m/s at the Nov-25
     transect).
  3. *3-parameter: ks + VELOCITY DIFFUSIVITY + wall roughness* (Sobol 16+9,
     extended to 45 runs to converge RE): posterior **ks = 0.429 m** (90% CI
     [0.396, 0.447]), while **diffusivity and wall roughness stay
     unconstrained** (near-flat marginals = prior). The two added parameters
     are **non-identifiable** from this data: they cannot absorb the
     near-bank velocity over-prediction (residual +0.03 m/s Sept, +0.22 m/s
     Nov at the posterior median), so ks stays high. RE converged to ~2.1
     nats (noisy at the +/-5% level from rejection-sampling). Plots:
     `auto-saved-results-HydroBayesCal/plots/posterior-3param-final.png`.

  **Conclusion of the 2D path:** water level is well-calibrated (RMSE 2-3 cm);
  the single-zone channel resistance settles at an *effective* ks ~ 0.43 m -
  inflated by a near-bank velocity bias that grain roughness, eddy viscosity
  and wall friction all fail to remove (measured log-law profiles give grain
  ks ~ 0.09 m). The residual is a 2D depth-averaged representation limit at
  the slow wadeable margins, compounded by the wetted-margin bathymetry
  bias - not a calibration-parameter problem. This motivates the 3D path.

## TELEMAC-3D non-hydrostatic calibration (active)

The 2D depth-averaged model cannot represent the slow near-bed / near-bank
velocities the FlowTracker sampled. The **20 multi-depth ADV verticals**
(item 3, three depths each) are genuine vertical-profile ground truth, so a
**non-hydrostatic TELEMAC-3D** model - which resolves the vertical velocity
structure and secondary currents - is calibrated against the velocity **at
each measured depth** rather than a depth-averaged proxy. The 3D case reuses
the 2D horizontal mesh, hotstarts from `r2d.slf`, and (per aXqua's 3D
extension) uses a single representative `FRICTION COEFFICIENT FOR THE BOTTOM`
(3D has no zonal friction file), sigma layers sized to `dz ~ dx/2`, and MURD
PSI advection for wetting/drying robustness. See `add3d.py` /
`axqua/threed.py`.

## Boundary conditions

Inflow Q per flow (prescribed flowrate), outflow stage from the synthetic
normal-flow rating (`stage_discharge`); at these discharges the rating stage
lies below the natural outfall water level, so the outflow effectively acts as
a free outfall - the reach is insensitive to the exact rating value (verified
by the +0.30 m sensitivity run).
