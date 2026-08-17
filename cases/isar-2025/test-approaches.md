---
author: Sebastian Schwindt
date: 2026-08-11
---

# Internal loss/gain (percolation)

The challenge of this case is to model the percolation zone between the main river branch and the small side channel on the left that gains approx. 0.065 m3/s at its outlet; tiny discharge, huge ecologically impact, so we want to have that in the model.

## Input files

The following `/geodata/` files served for the mesh generation:

* `baffles.gpkg` contains lines for metering discharge during simulations
* `dem-initial-roi.tif` the DEM snippet I used
* `liquid-boundaries.gpkg` has liquid boundaries, incl. internal loss `int-outflow-lose` and gain `int-inflow-gain` boundary lines (0.065 m3/s)
* `mesh-zones.gpkg` baseline for mesh size definition and extent
* `percolation-zone.gpkg` is an attempt to define a porous patch across the gravel deposit between the main and sidel channels (in addition to internal loss and gain boundary lines); used for green-ampt option
* `roughness-zones.gpkg` + `roughness-table.csv` are Hannah's roughness zones (not the most final one but this is probably not the biggest issue here)


## Modeling approaches

The rather porous gravel accumulation dates back to the 2024 summer flood; I designed it so that the ~0.065 m3/s percolate beneath its surface within roughly the top 0.5 m (defined by `porous depth (m)` in `percolation-zone.gpkg`). Water leaves the main channel at the deposits upstream edge (`int-outflow-lose`), travels under the bar, and resurfaces at `int-inflow-gain`, which feeds the side channel. Getting water into the side channel is the point of the whole exercise; so my minimum goal was that the steady run must reach a stable boundary-flux balance with that exchange active.

**Status: SOLVED.** Use **`percolation.mode: fortran`** with
**`percolation.losing_region: line`** and a **12 m** strip width - a generated
`USER_RAIN` routine that withdraws the exchange *depth-limited* from the channel
at the losing line and reinjects exactly what it took at the gaining line. It runs
stably (where every fixed-rate sink blew up), delivers the full 0.065 m3/s, and
**measurably wets the side channel** (33.9 % vs the 29.8 % baseline). See the
[summary table](#summary-of-attempts) and [the conclusions](#the-two-conclusions).

> This file is a living log. **Append a new "Attempt" section after every run**, and
> keep the summary table at the top in sync. Anyone (human or model) picking this up
> should be able to reproduce every row from the commands given.

---

## Summary of attempts

| # | Date | What was tested | Key setting(s) | Result | Verdict |
|---|------|-----------------|----------------|--------|---------|
| 1 | 2026-07-17 | Reach **without** internal exchange | prewet 1.0 m, duration 25000 s | Converged: imbalance ~0.4 %, domain volume flat at 3429 m3 | ✅ baseline works |
| 2 | 2026-07-27 | Internal exchange as **point sources** on ~1.4 m strips | prewet 1.0 m, duration 2500 s, hand-edited .cas | **Stalled.** 4 days wall time, sim time frozen at T = 2383 s, dt collapsed to 1e-5…3e-4 s, outflow flux ~1e-4 instead of -2.4 m3/s | ❌ unusable |
| 3 | 2026-08-01 | Diagnosis + rebuild of the mechanism (no run) | see [Mechanisms](#the-three-mechanisms-available) | Region sources, percolation-patch mode and USER_RAIN Fortran implemented; unit-tested | ✅ ready to test |
| T1 | 2026-08-01 | **Control:** does the pre-wet fix alone converge? | prewet 0.30 m, duration 4000 s, **no** internal lines | **Ran the full 4000 s and CONVERGED to -0.074 %** (volume flat at 2910 m3, dt 0.0869 s). Side-channel baseline: 29.8 % wetted, mean depth 0.0160 m | ✅ baseline healthy |
| T2 | 2026-08-01 | Internal exchange as **3 m buffered source regions** | prewet 0.30 m, `percolation.mode: off` | **Blew up in 28 s** at the losing line: U = ±1113 m/s on 3 adjacent nodes | ❌ sink too concentrated |
| T3 | 2026-08-01 | Losing exchange over the **whole percolation patch** | `percolation.mode: region` (2172 m2) | Survived 9x longer (255 s); **nothing wrong at the sink**, but 3 nodes blew up 177 m away near the ROI edge | ⚠️ sink fixed, separate local instability |
| T4 | 2026-08-01 | **USER_RAIN** depth-limited, uniform over patch | `percolation.mode: fortran`, `losing_region: patch` | **Ran the full 4000 s and CONVERGED to -0.757 %** - but the side channel is unchanged vs T1 (29.6 % vs 29.8 % wetted): delivery decays as the patch dries | ✅ stable / ❌ no effect |
| T5 | 2026-08-01 | **USER_RAIN** normalised over the **wet** patch area | `percolation.mode: fortran`, `losing_region: patch` | Delivers the full 0.0650 m3/s while the patch is wet, but **the patch dries out**: wet area 1044 → 8 m2, delivery decays to 0.0172 m3/s | ⚠️ stable, but the patch cannot supply it |
| T6 | 2026-08-02 | **USER_RAIN** over a **12 m strip along the losing LINE** | `losing_region: line`, width 12 m | **Converged to -0.167 % over the full 4000 s, delivering 0.0650 m3/s throughout**; wet area plateaus at 178 m2; **side channel 33.9 % wetted / 0.0219 m vs the baseline's 29.8 % / 0.0160 m** | ✅ **USE THIS** |

### The two conclusions

**1. The sink must be depth-limited.** The velocity that supplies a fixed
withdrawal is `U ~ Q / (H x width)`, which **diverges as `H → 0`**. Widening the
region only postpones the blow-up - 92 m2 died at 28 s, 2172 m2 at 255 s. Tapering
the withdrawal off with the local depth removes the divergence entirely (T4 ran 6x
longer than T3 and converged). **Use `percolation.mode: fortran`.**

**2. The withdrawal must come from the channel, not from the bar.** The bar is dry
on top by design - the water goes *under* it - so there is no surface water there
to take. Measured on the converged run with *no* extraction, the bar holds ~88 m2
of wet area out of 2172 m2 (median depth 0.2 mm); drawing from it delivered the
target only until the pre-wet surplus drained, then decayed to 0.008 m3/s and
**left the side channel unchanged** (29.3 % vs the 29.8 % baseline). The losing
**line** sits in the wetted channel (median ~0.07 m) where the water actually
infiltrates, and the through-flow replenishes what is withdrawn.
**Use `percolation.losing_region: line` with a ~12 m width** (~178 m2 wet at
steady state, 0.36 mm/s needed - comfortably inside every cap): full delivery
sustained, and the side channel goes to **33.9 % wetted / 0.0219 m**.

**Success criteria** used for every rung:

* outflow boundary flux settles at **-2.4 m3/s ± 1 %** (the internal exchange is
  net-zero, so it must not change the reach's overall balance);
* domain volume plateaus (no endless filling or draining);
* the adaptive time step recovers to **O(0.05-0.25 s)** at Courant 0.6 - a dt of
  1e-4 s is the signature of the failure in attempt 2;
* no sustained `GRACJG EXCEEDING MAXIMUM ITERATIONS` or
  `EXTREMLY HIGH VALUE OF FRICTION` storms in the listing;
* finally: **water depth > 0 in the side channel** downstream of the gaining line.

---

## Why attempt 2 stalled (the diagnosis)

Read from the 977 MB `.sortie` of
`axqua-case/simulation/steady2d.cas_2026-07-27-09h33min26s/`:

```
VOLUME IN THE DOMAIN :    6408.168     M3
FLUX BOUNDARY    1:    0.8000017     M3/S     <- inflow RB, exact
FLUX BOUNDARY    2:   -0.1224490E-03 M3/S     <- OUTFLOW: essentially ZERO
FLUX BOUNDARY    3:     1.600035     M3/S     <- inflow LB, exact
RELATIVE ERROR IN VOLUME AT T = 2383. S :  0.1111273E-14
MAXIMUM COURANT NUMBER:    0.3922552E-03
TIME-STEP                 :   0.3455190E-03
```

Four things stand out, in order of importance:

> **REVISED after T1/T2 (2026-08-01) - read this first.** The original ranking below
> put the over-deep pre-wet first. **That was wrong.** Two measurements settle it:
>
> * T1 starts at **6158 m3** with `prewet_depth: 0.30` - only ~4 % below the 6408 m3
>   of the stalled run, so the pre-wet depth barely changes the starting volume
>   (the seed is a smoothed surface 0.30 m over the *thalweg*, still up to 1.51 m
>   deep locally);
> * T1 then **drains that surplus cleanly** (6158 → 3637 m3 and falling) while its
>   time step *grows*, whereas T2 - identical except for the sink - blew up in
>   28 seconds.
>
> **Corrected ranking: the sink is the primary cause; the over-deep pre-wet is only
> an aggravator.** In attempt 2 the sink collapsed dt within the first seconds,
> which is precisely what prevented the surplus from ever draining and kept the
> thin-film friction warnings alive. Keep `prewet_depth: 0.30` anyway - it is
> closer to the physical flow depth and saves drain-down time - but do not expect
> it to fix stability on its own. Cause 2 is the one that matters.

**1. The pre-wetting was too deep (aggravator, NOT the primary cause - see above).**
`prewet_depth` was **1.0 m** on a reach whose steady flow depth is ~0.27 m. The
stalled run held **6408 m3** of water where the converged run of attempt 1 held
**3429 m3** - nearly twice as much. That surplus sits as thin films on the margins
and floodplain and drains very slowly. Because the roughness zones run up to
**ks = 0.5 m**, i.e. larger than the film depth itself, TELEMAC emits thousands of
`EXTREMLY HIGH VALUE OF FRICTION` warnings and the linear system becomes badly
conditioned. On top of that, `duration` had been cut from 25000 s to **2500 s**, so
even a healthy run could not have drained the surplus and balanced in time.
*(`case-config-Mint.yml` already carried this diagnosis and the 0.30 m fix - it had
simply not been carried over to the main config.)*

**2. A concentrated sink dries cells, and that is what collapsed the time step.**
This is the mechanism behind the frozen clock. Two facts from the TELEMAC v9.1.1
sources:

* `prosou.f` applies a source as `SMH = SMH + Q/area` with **no depth limiting
  whatsoever** for a *negative* source. Nothing stops a sink from pulling a cell
  below zero depth; only `TREATMENT OF NEGATIVE DEPTHS` cleans up afterwards.
* the adaptive time step (`telemac2d.F:227-243`) is computed from `CFLPSI(U,V)`
  **only** - sources are not part of the CFL condition.

So a sink dries a cell, the drying spikes the local velocity, the CFL controller
reacts by halving dt, and because the sink keeps extracting at the same rate the
cell re-dries every step. dt gets pinned at 1e-4 s indefinitely. The mass balance
stays perfect (`RELATIVE ERROR IN VOLUME` = 1e-15) - it is a *stability* failure,
not a conservation failure.

**3. Hand-edits to the generated `.cas` made it worse.**
`IMPLICITATION FOR DEPTH/VELOCITY` had been lowered 0.80 → 0.60 (less damping,
exactly where a wetting/drying front needs more), and `CONTROL OF LIMITS` /
`LIMIT VALUES` had been commented out (the divergence guard removed). **Both were
edits to a generated file, which any rebuild silently reverts.** They are now
config keys (`hydrodynamics.implicitation`, `hydrodynamics.control_of_limits`), so
the choice is reproducible.

**4. The `GRACJG EXCEEDING MAXIMUM ITERATIONS 50` storm was the turbulence solver,
not the free-surface solver.** The free-surface solver is set to 200 iterations in
the `.cas`; the **50** is the default of
`MAXIMUM NUMBER OF ITERATIONS FOR K AND EPSILON`, which - per the TELEMAC
dictionary - also governs the **Spalart-Allmaras** solve. aXqua now raises it
(120) for model 6 as well as for k-epsilon.

### What was *not* the problem

* **The 2-region / node-count limit.** `MAXIMUM NUMBER OF SOURCES : 2` was correct:
  that keyword caps the number of *regions*, while
  `MAXIMUM NUMBER OF POINTS FOR SOURCES REGIONS` caps *vertices per polygon*. The
  node arrays are dimensioned `PT_IN_POLY(MAXSCE, NPOIN)`, so hundreds of nodes per
  region are fine. The old file was dimensionally valid.
* **The outflow rating.** Attempt 1 converged against the same rating curve.
  Rung T1 re-tests this for free.

---

## The three mechanisms available

All three are implemented and unit-tested; switching between them is a config
change plus a rebuild. Per-node severity is what distinguishes them - the same
0.065 m3/s spread over a larger area is a gentler depth rate and therefore less
able to dry a cell:

| Mechanism | Config | Area of the losing zone | Withdrawal rate | Can it dry a cell? |
|-----------|--------|------------------------|-----------------|--------------------|
| point sources (attempt 2, **removed**) | - | a handful of nodes | ~0.1 m/s locally | **yes, immediately** (T2-style blow-up) |
| thin buffered regions | `percolation.mode: off` | 92 m2 (3 m strip, 449 nodes) | 0.71 mm/s | **yes** - killed T2 in 28 s |
| percolation patch as region | `percolation.mode: region` | 2172 m2 (the whole patch) | 0.030 mm/s | not at the sink (T3 survived 9x longer) |
| USER_RAIN Fortran | `percolation.mode: fortran` | 2172 m2, **depth-limited** | 0.062 mm/s over the *wet* part | **no, by construction** |

(Areas are the values aXqua reports at build time; the patch region is ~24x
gentler per unit area than the 3 m strip.)

### 1. Buffered source regions (default)

Each `int-*` line is buffered to a strip
`boundaries.internal_source_region_width` wide (default **3.0 m**) and written as a
TELEMAC **source region**:

```
SOURCE REGIONS DATA FILE : source-regions.txt
MAXIMUM NUMBER OF SOURCES : 2
MAXIMUM NUMBER OF POINTS FOR SOURCES REGIONS : 11
WATER DISCHARGE OF SOURCES : -0.065;0.065
TYPE OF SOURCES : 1
```

TELEMAC assigns every mesh node inside each polygon to that region and spreads the
discharge uniformly over the enclosed area. Two rules that are easy to get wrong:

* **never** emit `ABSCISSAE OF SOURCES` / `ORDINATES OF SOURCES` together with the
  region file - `lecdon_telemac2d.f` checks the coordinate route *first*, so the
  region file would be silently ignored;
* `TYPE OF SOURCES` must stay **1**. With regions, type 2 (Dirac) adds the *full*
  discharge at *every* node of the region - the total would be multiplied by the
  node count.

### 2. Percolation patch as the losing region

`percolation.mode: region` uses the polygon in
`user-sources/geodata/percolation-zone.gpkg` (patch `main-side`, ~110 x 15 m,
2172 m2, `porous depth (m)` = 0.5) as the **losing** region instead of the thin
strip. The gaining line keeps its buffered strip. Same physics, ~24x gentler per
node, still pure keywords - no compiler needed. This is the natural way to use the
new polygon.

The patch outline is simplified to at most `boundary.MAX_REGION_VERTICES` (16)
vertices - 22 → 11 here - because TELEMAC tests every mesh node against the raw
vertex list and the `.cas` must declare
`MAXIMUM NUMBER OF POINTS FOR SOURCES REGIONS` accordingly.

### 3. USER_RAIN percolation Fortran (`percolation.mode: fortran`)

Generated by `axqua/fortran.py` into `<model_dir>/user_fortran/user_rain.f` and
referenced by `FORTRAN FILE`; TELEMAC compiles it at run time. It hooks
`USER_RAIN`, the spatialised-rain routine called from `prosou.f`, which is the only
place a **depth-aware** distributed source can be injected. Requires
`RAIN OR EVAPORATION : YES` with a base rate of 0 (aXqua writes both).

What it does each time step (**two passes** - the second version, see T4 vs T5):

1. **pass 1** - sum the *taper-weighted wet area* of the losing patch, where the
   taper is `F = clamp((h - min_depth)/taper_depth, 0, 1)`, combined across MPI
   subdomains with `P_SUM`;
2. **pass 2** - withdraw `Q_target * F_i / wet_area` at each node, capped three
   ways: by the taper `F`, by an absolute ceiling `percolation.max_rate`, and by
   **half the water available above `min_depth` this step** - so extraction always
   stops before a cell can dry;
3. sum what was *actually* extracted and reinject **exactly that amount** over the
   gaining region.

Normalising by the **wet** area (step 1) rather than the whole polygon is what
makes the full target reachable: on this patch **47.5 % of the polygon is drier
than 5 cm**, so a uniform `Q/area` over the whole polygon silently throws that
share away and delivers only ~48 % of the target (that was T4; T5 is the fix).

Mass is exact by construction, and TELEMAC audits it independently (`PLUIE` is
integrated into the mass balance, visible in the listing). Defaults:
`min_depth` 0.05 m, `taper_depth` 0.05 m, `max_rate` 0.001 m/s.

**It still cannot conjure water that is not there.** If the wet area shrinks so
far that even the ceiling cannot supply the target, the routine delivers what is
physically available and says so. That is why it prints, every 500 calls:

```
USER_RAIN PERCOLATION: CALL  500  DELIVERED  6.5E-02 M3/S OF TARGET  6.5E-02 M3/S
   WET AREA  1054. M2
```

**Always read that line before trusting a percolation run** - a silently
under-delivering (or zero) exchange looks perfectly stable and would quietly fail
the whole purpose of the setup.

### Why TELEMAC's own Green-Ampt cannot be used

TELEMAC-2D ships `RAINFALL-RUNOFF MODEL : 3` (Green-Ampt), plus Horton (2) and
SCS-CN (1). None of them can drive this exchange. From `runoff_greenampt.f`:

```fortran
ACCROFF = ACCR - ACCINF                          ! rainfall minus infiltration
RAIN_MPS_GEO = (ACCROFF(I) - ACCROF_OLD(I))/DT
PLUIE%R(I) = MAX(RAIN_MPS_GEO, -MAX(HN%R(I),0)/DT)
```

`ACCR` accumulates **rainfall** (`RAIN_MPS`). With
`RAIN OR EVAPORATION IN MM PER DAY : 0.` - our case - `ACCR` stays zero, so
`ACCROFF` stays zero and `PLUIE = 0`. **No rain, no infiltration.** It is a
rainfall-*abstraction* model: it decides how much of *falling* rain becomes
runoff. It cannot take water out of a wetted riverbed. It is also a **pure sink**
(infiltrated water leaves permanently, with no mechanism to return it at the
gaining line), and enabling it would require rain across the whole domain.

Two things about it *are* worth knowing:

* **`KS` is spatial.** It is a `TYPE(BIEF_OBJ)` read from `FORMATTED DATA FILE 2`
  and interpolated onto the mesh by `HYDROMAP` - so TELEMAC does support a
  conductivity map, even though the driver is rainfall.
* **Units:** `FCAPA = FCAPA*KS/3600/1000` means TELEMAC's `KS` is in **mm/h**,
  not m/s.

**What we do instead:** reuse the Green-Ampt *formula* inside `USER_RAIN`
(`percolation.conductivity`), so the withdrawal is computed from conductivity and
water level rather than prescribed - see
[the two exchange modes](#the-two-exchange-modes-prescribed-vs-green-ampt).

### Why not culverts (`BUSE`)?

The obvious alternative - a culvert linking the losing to the gaining line - was
rejected after reading `buse.f`: in v9.1.1 the culvert discharge is **always**
computed from the head difference between the two ends (`DBUS` from S1 - S2, with
relaxation). There is no fixed-discharge culvert type, so it cannot deliver a
prescribed 0.065 m3/s. Culverts are also single-node at each end, which is exactly
the concentration that failed in attempt 2. (They *are* depth-guarded, which is
where the `USER_RAIN` taper idea came from.)

---

## The two exchange modes: prescribed vs Green-Ampt

Once the *mechanism* was settled (depth-limited `USER_RAIN` drawing from a 12 m
strip along the losing line), there are two ways to decide **how much** water is
exchanged. Both were run end to end through the normal workflow.

| | **A - prescribed** | **B - Green-Ampt (conductivity)** |
|--|--------------------|-----------------------------------|
| config | `case-config.yml` | `case-config-greenampt.yml` |
| tree | `axqua-case/scenarios/prescribed-q/` | `axqua-case/scenarios/green-ampt/` |
| exchange | fixed 0.065 m3/s (the gpkg's `Target flow`) | `f = kf*(h + Lz + hf)/Lz` per node |
| parameters | - | `kf` 3.0e-4 m/s, `Lz` 0.5 m (patch attribute), `hf` 0.2 m |
| responds to stage? | **no** - same at 2.4 and 20 m3/s | **yes** |
| delivered at steady state | 0.0650 m3/s | **0.1020 m3/s** |
| wet area | 177.9 m2 | 176.6 m2 |

**Both converged over the full 4000 s and both feed the side channel:**

| | volume [m3] | Q_out | imbalance | side channel wetted | mean depth |
|--|-------------|-------|-----------|---------------------|------------|
| T1 baseline (no exchange) | 2910.5 | 2.4018 | -0.074 % | 29.8 % | 0.0160 m |
| **A - prescribed 0.065 m3/s** | 2992.7 | 2.4047 | **-0.196 %** | **33.9 %** | **0.0217 m** |
| **B - Green-Ampt 0.102 m3/s** | 3015.9 | 2.4038 | **-0.157 %** | **34.4 %** | **0.0234 m** |

Note the response is **strongly sub-linear**: B moves 57 % more water than A but
gains only 0.5 percentage points of wetted area and ~8 % more mean depth. The side
channel's own conveyance limits how much extra depth the additional supply can
produce, so the result is not very sensitive to the exact exchange rate - which is
reassuring for calibration.

**The two formulations agree to a factor of 1.57.** Running B back through the formula,
the effective rate is `0.102/176.6 = 5.78e-4 m/s`, which implies a taper-weighted
mean depth of **h ~ 0.26 m** in the strip - essentially the **0.266 m** normal
depth the rating curve gives for Q = 2.4 m3/s. The formulation is seeing the right
water.

**Calibrated conductivity: `kf ~ 1.9e-4 m/s` reproduces the 0.065 m3/s target**
(= 3.0e-4 / 1.57), i.e. ~690 mm/h in TELEMAC's units. That sits inside the
somewhat-clogged gravel band (1e-4..1e-3 m/s) and is now a *measured* number
rather than a literature guess.

### Which to use

* **Scenario A (prescribed)** if 0.065 m3/s is a trusted field observation for
  *this* discharge and you want the model to honour it exactly.
* **Scenario B (Green-Ampt)** if you intend to run other discharges or a
  hydrograph - percolation physically scales with stage, and a fixed 0.065 m3/s
  would be wrong at any flow other than the one it was measured at. It also gives
  HydroBayesCal a **physically meaningful calibration parameter** (`kf`) instead of
  a hard-coded discharge.

Set `percolation.conductivity` to switch; leave it unset for the prescribed mode.

### Reference: k_f for river gravel beds

| bed condition | k_f [m/s] | TELEMAC `KS` [mm/h] |
|---------------|-----------|---------------------|
| open-framework / clean cobble gravel | 1e-1 .. 1 | 3.6e5 .. 3.6e6 |
| clean gravel | 1e-2 .. 1e-1 | 3.6e4 .. 3.6e5 |
| sandy gravel | 1e-3 .. 1e-2 | 3.6e3 .. 3.6e4 |
| **moderately clogged (colmated)** | **1e-4 .. 1e-3** | **360 .. 3600** |
| strongly colmated / silted | 1e-7 .. 1e-5 | 0.36 .. 36 |

**A caution on interpreting the 0.065 m3/s.** Treated as *lateral Darcy flow*
through the bar cross-section it is not credible: with the measured head drop
(WSE 817.351 m at the losing line, 816.834 m at the gaining line, i.e.
Δh = 0.518 m over a 109.6 m path → i = 0.0047) and a 0.5 m x 30.6 m section,
`kf = Q/(i*A) = 0.9 m/s` - open-conduit territory. Treated as **infiltration over
an area** it is entirely reasonable (1.9e-4..3.7e-4 m/s). So read the 0.5 m
"porous depth" as the near-surface layer *through which water infiltrates*, not as
the conveying cross-section; the underflow presumably uses a thicker gravel body.

## How to run the ladder

Each rung builds into its **own** folder (`axqua-case/ladder-<rung>/`), so
nothing overwrites the production `simulation/` case and rungs can be compared
side by side:

```bash
cd /srv/private/axqua
export PYTHONPATH=src        # axqua is not pip-installed in axqua-env here

# one rung, build + run (add --ncsize N to change the core count)
mamba run -n axqua-env python cases/isar-2025/ladder.py T2

# build only, e.g. to inspect the generated .cas / source-regions.txt first
mamba run -n axqua-env python cases/isar-2025/ladder.py T3 --build-only
```

Each rung writes `rung.json` (settings + the flux verdict). What every rung changes
relative to `case-config.yml` is listed explicitly in the `RUNGS` dict at the top of
`ladder.py` - nothing is hidden.

**Note on `pythomac`:** aXqua's `analyze_flux_convergence` (and therefore
`initial_run.py`'s convergence report and the `extracted-fluxes.csv` /
`flux-convergence.png` products) needs **pythomac**, which is **not installed on
this machine** in any environment - so that step is skipped with a message. To get
it back: `pip install pythomac`, or set `PYTHOMAC_DIR` to a local checkout.
Meanwhile `ladder.py` carries a dependency-free fallback that parses the
`BALANCE OF WATER VOLUME` blocks out of the `.sortie` directly:

```bash
python cases/isar-2025/ladder.py T2 --summary     # read-only, no rebuild
```

```
boundary-flux summary (1 printouts, final T=28.2 s):
     T [s]      volume      Q_in     Q_out   imbalance    dt [s]
      28.2      6318.8    2.4008    7.3715  -207.048 %   0.05448
VERDICT: FAILED - LIMIT VALUES TRESPASSED (divergence guard fired)
```

(`Q_in` = 2.4008 confirms the per-line inflow prescription is exactly right;
`Q_out` = 7.37 is the pre-wet surplus draining, which is expected this early.)

**Also note:** `axqua-env` has no `pytest`/`ruff`, so the test suite was run
from the base conda environment (which has every runtime dependency). The solver
runs use `axqua-env` as documented.

**Watching a running rung.** The interesting signal is the time step, not the
iteration count:

```bash
grep -E "TIME-STEP|COURANT" <rung-dir>/steady2d.cas_*/*.sortie | tail -20
grep -A6 "BALANCE OF WATER VOLUME" <rung-dir>/steady2d.cas_*/*.sortie | tail -20
```

A dt that keeps falling below ~1e-3 s means the sink is drying cells again: stop
the rung, record it here, and escalate to the next mechanism.

---

## Promoting the winning rung to production

Once a rung balances, put its setting into `case-config.yml` and rebuild the real
case (`axqua-case/simulation/`) - the ladder folders are scratch:

1. set `percolation.mode` in `case-config.yml` to the winning mode
   (`off` = 3 m strips, `region` = percolation patch, `fortran` = USER_RAIN);
2. raise `hydrodynamics.duration` to comfortably past the time the rung needed to
   balance (read it off the rung's flux summary, then add ~50 %);
3. rebuild and run:

   ```bash
   mamba run -n axqua-env python cases/isar-2025/preprocessing.py
   mamba run -n axqua-env python cases/isar-2025/initial_run.py
   ```

4. **check the side channel is actually fed** - the whole point. `ladder.py
   <rung> --summary` now reports the wetted fraction and mean depth in a 40 m
   neighbourhood of the gaining line, straight out of `r2d.slf`:

   ```
   side channel around the gaining line (r=40 m, 27541 nodes):
     wetted (h>1 cm): 14274 nodes (51.8 %)
     mean depth     : 0.1127 m   (wet only: 0.2171 m)
     max depth      : 1.0994 m
   ```

   **Compare a percolation rung against T1** (which carries no exchange at all): if
   the mechanism is doing its job, the gaining neighbourhood is measurably wetter.
   Confirm visually too - open `r2d.slf` in ParaView/QGIS around the gaining line
   (about 677121.6, 5268015.8 to 677162.0, 5268035.8);
5. in `fortran` mode, also verify **how much** water is actually being moved: the
   routine delivers less than 0.065 m3/s while the patch is shallow. `PLUIE` is
   part of TELEMAC's mass balance, so the listing's
   `BALANCE OF WATER VOLUME` block accounts for it - compare the domain volume
   trend with and without the exchange if in doubt.

Only after that does it make sense to move on to the mesh-convergence study and
HydroBayesCal.

---

## Attempt log

### Attempt 1 - 2026-07-17 - reach without internal exchange ✅

**Hypothesis:** the base hydraulic setup (mesh, boundaries, rating) is sound.

**Setup:** no `int-*` lines in the model; prewet 1.0 m; duration 25000 s.

**Result:** converged. Final row of `extracted-fluxes.csv`:

```
Time     Volumes    B1         B2          B3
25000.0  3429.378   0.9931294  -2.409365   1.406926
```

In (0.993 + 1.407) vs out (-2.409): imbalance ~0.4 %; domain volume flat at
3429 m3. Relative imbalance oscillated 0.0017-0.021 %.

**Verdict:** baseline hydraulics work. Note the inflows settled at 0.993/1.407
rather than the prescribed 0.8/1.6 - this predates the per-line inflow discharges.

**Next:** add the internal exchange.

---

### Attempt 2 - 2026-07-27 - point sources, over-wet start ❌

**Hypothesis:** the internal exchange can be added as distributed point sources.

**Setup:** `int-outflow-lose` -0.065 / `int-inflow-gain` +0.065 m3/s over ~1.4 m
buffer strips (222 / 332 nodes); prewet **1.0 m**; duration cut to 2500 s;
`.cas` hand-edited (implicitation 0.60, `CONTROL OF LIMITS` commented out).

**Command:** `python cases/isar-2025/initial_run.py` (16 cores)

**Result:** ran ~4 days of wall time without finishing. Simulated time froze at
T = 2383 s of 2500 s; dt collapsed to 1e-5…3e-4 s (max Courant 1e-7…6e-4 against a
target of 0.6); the outflow boundary carried ~1e-4 m3/s instead of -2.4; domain
volume crept up 6407 → 6408 m3; thousands of `GRACJG EXCEEDING MAXIMUM ITERATIONS
50` and `EXTREMLY HIGH VALUE OF FRICTION` warnings. Global mass conservation was
perfect throughout (1e-15).

**Verdict:** unusable. See [the diagnosis](#why-attempt-2-stalled-the-diagnosis) -
over-wet start (dominant) plus a sink concentrated enough to dry cells and pin the
time step.

**Note for the record:** the `.cas` of this run used a `SOURCE REGIONS DATA FILE`
formulation that existed **only** in that generated file - it was never committed
to `src/`, so any rebuild would have regenerated the older, worse point-source form
(`ABSCISSAE/ORDINATES OF SOURCES`). That is now fixed: the region route lives in
`axqua/steering.py::write_source_regions` and is covered by tests.

**Next:** fix the initial condition, spread the sink, and make the concentration a
tunable knob.

---

### Attempt 3 - 2026-08-01 - diagnosis and rebuild (no solver run) ✅

**What changed in the code** (all unit-tested, `pytest tests/` = 113 passed):

* `boundary.py`: internal lines now produce `InternalSourceRegion` polygons
  (buffered strips, or the percolation patch) instead of per-node point sources;
  a region with no mesh node inside raises at build time (TELEMAC would abort).
* `steering.py`: `write_source_regions()` writes `source-regions.txt`; `write_cas`
  emits the region keyword block, or the `FORTRAN FILE` + `RAIN` keywords in
  fortran mode - never both, and never the coordinate form.
* `fortran.py` (new): generates the depth-limited `USER_RAIN` routine.
* `config.py`: new `percolation` block; `boundaries.internal_source_region_width`;
  `hydrodynamics.control_of_limits`; the SA solver now gets the raised iteration
  cap.
* `unsteady.py`: the unsteady case now carries the internal exchange too - it was
  silently dropping it, so a hydrograph run would have lost the side-channel feed.
* `case-config.yml`: prewet 1.0 → **0.30 m**, duration 2500 → **4000 s**,
  implicitation and the limits guard pinned explicitly in config.

**Verified against the TELEMAC v9.1.1 install** (`sources/telemac2d/`): the region
file format and keyword semantics, the absence of a depth guard on negative
sources, the CFL controller ignoring sources, `TYPE OF SOURCES` 1 vs 2, the
`USER_RAIN` hook and its `PLUIE` mass audit, and the culvert rejection.

**Verdict:** ready to test. Ladder rungs T1-T4 defined.

---

### Attempt T1 - 2026-08-01 - control: pre-wet fix alone

**Hypothesis:** with prewet 0.30 m the reach converges again, confirming the
initial condition was the dominant cause and clearing the outflow rating of
suspicion.

**Setup:** internal lines dropped from the layer (`ladder.py` writes a filtered
GeoPackage), prewet 0.30 m, duration 4000 s, implicitation 0.80, limits guard on,
8 cores.

**Command:** `python cases/isar-2025/ladder.py T1 --ncsize 8`

**Result:** _running, converging cleanly._ The reach drains its pre-wet surplus
monotonically toward the attempt-1 equilibrium while the time step **grows**:

| T [s] | volume [m3] | Q_in | Q_out | imbalance | dt [s] |
|-------|-------------|------|-------|-----------|--------|
| 152.4 | 5598.9 | 2.4000 | 9.6305 | -301 % | 0.0605 |
| 306.0 | 4510.8 | 2.4000 | 8.4256 | -251 % | 0.0641 |
| 440.1 | 3859.6 | 2.4000 | 6.0977 | -154 % | 0.0707 |
| 513.7 | 3636.8 | 2.4000 | 4.9101 | -105 % | 0.0757 |
| 1708.0 | 3000.9 | 2.4000 | 2.5008 | -4.20 % | 0.0867 |
| 2273.0 | 2961.2 | 2.4000 | 2.4397 | **-1.65 %** | 0.0869 |

| 3968.0 | 2910.5 | 2.4000 | 2.4018 | **-0.074 %** | 0.0869 |

**✅ CONVERGED** - the run completed its full 4000 s and finished at **-0.074 %**,
well inside the <1 % criterion, with the volume essentially flat (2910 m3, drifting
~0.02 m3/s). The time step plateaus at **0.0869 s** - the Courant-0.6 ceiling for
this mesh (shortest edge 0.207 m). The imbalance alternates between ~-0.1 % and
~-1.4 % from printout to printout; that is ordinary noise on a live steady state,
not drift. **4000 s of simulated time is the right duration for this reach.**

**Side-channel baseline (this is the number the percolation rungs must beat).**
With no internal exchange at all, in a 40 m neighbourhood of the gaining line:

```
wetted (h>1 cm): 8194 nodes (29.8 %)
mean depth     : 0.0160 m   (wet only: 0.0516 m)
max depth      : 0.6275 m
```

(Do **not** compare against T2's reading of 51.8 % / 0.113 m - that run died at
T = 28 s, so it was still reporting the pre-wet initial condition, not a converged
state.)

Volume is heading straight for the **3429 m3** of attempt 1, `Q_out` is falling
towards the prescribed 2.4 m3/s, and `Q_in` sits at exactly 2.4000 (the per-line
0.8 + 1.6 prescription is spot on). Crucially the time step **rises** 0.060 →
0.076 s instead of collapsing.

**Verdict (interim): ✅ the baseline is healthy.** This settles the causal question
raised above: the pre-wet surplus drains perfectly well **provided dt stays
healthy**. In attempt 2 the sink collapsed dt within the first seconds, which is
what stopped the surplus from ever draining - so the ranking in the diagnosis
section is now: **the sink is the primary cause, the over-deep pre-wet only an
aggravator.**

---

### Attempt T2 - 2026-08-01 - internal exchange as 3 m source regions

**Hypothesis:** with a sane initial condition and the exchange spread over 3 m
strips (449 / 654 nodes, ~0.71 mm/s withdrawal), the run stays stable and the side
channel is fed.

**Setup:** as T1 but with the internal lines active, `percolation.mode: off`,
`internal_source_region_width: 3.0`, 8 cores.

**Command:** `python cases/isar-2025/ladder.py T2 --ncsize 8`

**Result: ❌ FAILED after 2 min 42 s** (500 iterations, T = 28.2 s) with

```
LOWER LIMIT ON U REACHED AT POINT  13758
 WITH COORDINATES    677055.5     AND     5267944.
 THE VALUE OF U IS    -1113.661     THE LIMIT IS:    -1000.000
 UPPER LIMIT ON U REACHED AT POINT  13759   ->  U = +1002.766
 LOWER LIMIT ON U REACHED AT POINT  13760   ->  U = -1033.764
LIMIT VALUES TRESPASSED, TELEMAC-2D IS STOPPED
```

Those three nodes are **the west end of the `int-outflow-lose` line**
(the line runs from 677055.33, 5267945.00 to 677085.67, 5267940.87), and the sign
**alternates between adjacent nodes** - a classic checkerboard instability driven
by the sink.

Everything else was healthy right up to the stop: dt = 0.054 s, Courant 0.19,
relative volume error 1e-15, the Spalart-Allmaras solve converging in 26 iterations
(the raised iteration cap works - no `GRACJG` storm, 2 occurrences in the whole run
versus thousands in attempt 2).

**Verdict:** spreading the sink over a 92 m2 strip is **not enough**. The reason is
physical, not numerical: the depth-averaged velocity needed to supply a fixed
withdrawal is `U ~ Q / (H x width)`, which **diverges as H → 0** no matter how large
the region is. The losing line sits on a gravel patch that is shallow (and partly
drying while the reach drains), so a *fixed-rate* sink will always blow up there.

**This is the key insight of the whole exercise:** the fix is not a bigger region,
it is a sink that **knows the local depth**. That is exactly what
`percolation.mode: fortran` does.

**Worth noting:** re-enabling `CONTROL OF LIMITS` (which had been hand-commented out
in attempt 2) converted a silent 4-day dt collapse into a clean 3-minute failure
that names the offending nodes and coordinates. Keep it on.

**Next:** T3 (same sink over the 24x larger patch - cheap to test, but expected to
merely delay the same divergence) and T4 (depth-limited, the principled fix).

---

### Attempt T3 - 2026-08-01 - losing exchange over the whole percolation patch ❌

**Hypothesis:** spreading the -0.065 m3/s over the 2172 m2 patch (0.030 mm/s, 24x
gentler than T2's strip) keeps the sink from drying cells.

**Setup:** as T2 but `percolation.mode: region`; 8 cores.

**Command:** `python cases/isar-2025/ladder.py T3 --ncsize 8`

**Result: ❌ FAILED at T = 255.4 s** - but **in a completely different place**:

```
UPPER LIMIT ON U REACHED AT POINT 18158
 WITH COORDINATES    677314.6     AND     5268125.
 THE VALUE OF U IS     1060.903
```

Distances from that point: **176.7 m from the gaining line and from the
percolation patch**, 293.8 m from the losing line, 92.9 m from the outflow, and
only **18.4 m from the ROI edge**. Just **3 nodes** trespassed.

| T [s] | volume | Q_in | Q_out | dt [s] |
|-------|--------|------|-------|--------|
| 141.7 | 5704.7 | 2.4000 | 9.4258 | 0.0563 |
| 198.1 | 5290.0 | 2.4000 | 9.9671 | 0.0561 |
| 255.4 | 4878.4 | 2.4000 | 9.3080 | 0.0573 |

**Verdict: the patch region DID fix the sink.** Nothing blew up at the losing
line this time - T2's failure mode is gone, and the run survived 9x longer
(255 s vs 28 s) while draining normally with a stable dt. What killed it is a
*separate*, highly localised (3-node) instability near the domain edge.

**Attribution is not yet settled - be careful here.** Two readings fit:

1. it is caused by the exchange - the gaining injection pushes water into the side
   channel, and the advancing wetting front there goes unstable ~177 m downstream;
2. it is a generic marginal-cell / wetting-drying artefact near the ROI edge that
   T1 simply has not hit (T1 passed T = 255 s and reached T > 1000 s cleanly, but
   T1 never wets that area because nothing feeds the side channel).

T4 discriminates: it injects at the same gaining line, so **if T4 survives past
T = 255 s, reading 1 is wrong** and the problem is a local mesh/terrain artefact
rather than the exchange mechanism.

> **Caveat on that discrimination.** It only holds if T4 is actually moving a
> meaningful discharge. The depth taper deliberately delivers *less* than
> 0.065 m3/s while the patch is shallow, so a routine that silently moves ~nothing
> would look perfectly stable **and** would fail the real goal. The generated
> routine therefore now prints its delivered discharge every listing printout:
>
> ```
> USER_RAIN PERCOLATION: DELIVERED   6.5E-02 M3/S OF TARGET   6.5E-02 M3/S
> ```
>
> (added after the T4 run below had already started, so that run does not carry it
> - for T4 the check is done on the result file instead, see its entry.)

**Next:** T4 (depth-limited USER_RAIN). If T4 also dies at ~677315, 5268125, the
remedy is local - pre-wet the side channel, refine/repair the mesh there, or relax
`CONTROL OF LIMITS` after confirming the rest of the field is sane - not another
change to the source mechanism.

---

### Attempt T4 - 2026-08-01 - USER_RAIN, depth-limited (uniform over the patch) ✅ stable, ⚠️ under-delivers

**Hypothesis:** a sink that tapers off with the local depth cannot dry a cell, so
it cannot blow up - regardless of how shallow the patch is.

**Setup:** `percolation.mode: fortran`, `min_depth` 0.05 m, `taper_depth` 0.05 m;
8 cores.

**First attempt failed to compile** - a genuine bug in the generator, worth
recording because it is easy to repeat: `USE DECLARATIONS_TELEMAC2D` imports a very
large namespace and the generated locals collided with it (`NREG` is TELEMAC's own
source-region counter, `MAXV` is a *derived type* there, and `HMIN` / `DEJA` / `F`
are module variables). The rejected declarations then cascaded into a wall of
"no IMPLICIT type" errors. **Fix:** every generated local now carries an `HMP_`
prefix, and `tests/test_source_regions.py::test_fortran_compiles` now compiles the
generated routine for real with

```bash
gfortran -fsyntax-only -ffixed-form -ffixed-line-length-72 \
  -I /home/modelling/telemac-v911/telemac-mascaret/builds/*/modules  user_rain.f
```

(skipped automatically where gfortran or a TELEMAC build is missing).

**Result: ✅ STABLE and CONVERGING.** Ran past **T = 1539 s** with **zero** limit
trespasses - 6x beyond where T3 died - and marching to steady state on the same
trajectory as the no-exchange baseline:

| T [s] | T1 volume | T4 volume | T1 Q_out | T4 Q_out |
|-------|-----------|-----------|----------|----------|
| ~440  | 3859.6 | 3839.0 | 6.098 | 5.853 |
| ~1500 | 3021.2 (T=1535) | 3015.3 (T=1494) | 2.504 | 2.538 |

At T = 1494 s the imbalance is **-5.7 %** and still tightening, with dt at
0.0913 s. **The depth-limited sink does not merely survive - it converges.**

**But it under-delivers.** Sampling the initial depth field inside the percolation
patch (10394 nodes):

| depth band | nodes | share |
|------------|-------|-------|
| h > 0.10 m (full extraction) | 4590 | 44.2 % |
| 0.05-0.10 m (partial) | 867 | 8.3 % |
| **h < 0.05 m (none)** | **4937** | **47.5 %** |

median depth in the patch is only **0.066 m**, and the area-mean taper is 0.485 -
so this version delivers **~0.031 m3/s of the 0.065 target (48 %)**. The cause is
the formulation, not the physics: it spread `Q/area` over the **whole** polygon and
then tapered, so the dry half of the patch simply lost its share.

**Final result: ran the full 4000 s and CONVERGED to -0.757 %** (volume 2893 m3,
dt 0.0933 s) - the headline stability result, where every fixed-rate variant blew
up within seconds.

**But it left the side channel unchanged:**

| | side channel wetted | mean depth |
|--|--------------------|------------|
| T1 (no exchange at all) | 29.8 % | 0.0160 m |
| **T4 (patch exchange)** | **29.6 %** | **0.0158 m** |

Identical within noise. T4 draws from the patch, so - like T5 - its delivery
decayed towards zero as the bar drained (T4 predates the diagnostic print, so this
is inferred from the identical outcome plus T5's measured decay). **Converged,
stable, and hydrologically inert.**

**Verdict:** depth-limiting is correct and completely cures the blow-up, but
drawing from the patch delivers nothing at steady state. → T5 (normalisation),
then T6 (the region itself).

**This also settles the T3 attribution:** T4 injects at the same gaining line, with
a real (non-zero) discharge, and sails past T = 255 s. So T3's 3-node blow-up near
the ROI edge was **not** caused by the exchange mechanism - it is a local
mesh/terrain artefact in that corner of the domain.

---

### Attempt T5 - 2026-08-01 - USER_RAIN normalised over the WET patch area

**Hypothesis:** normalising the target discharge over the *currently wet* taper-
weighted area (instead of the whole polygon) delivers the full 0.065 m3/s while
keeping every safety property of T4.

**The routine now does two passes per time step:**

1. sum the taper-weighted wet area of the losing patch (`P_SUM` across subdomains);
2. withdraw `Q_target * F_i / wet_area` at each node, capped three ways - by the
   taper `F`, by half the water available above `min_depth` this step, and by a new
   absolute ceiling `percolation.max_rate` (default **1 mm/s**).

The resulting rate is `0.065 / ~1054 m2` ≈ **0.062 mm/s** - 11x gentler than the
T2 strip that blew up, 16x below the ceiling, and never applied to a shallow cell.
The routine also prints its delivered discharge every listing printout:

```
USER_RAIN PERCOLATION: DELIVERED   6.5E-02 M3/S OF TARGET   6.5E-02 M3/S
```

**Setup:** `percolation.mode: fortran` with the revised routine; 8 cores.

**Command:** `python cases/isar-2025/ladder.py T5 --ncsize 8`

**Result: ✅ DELIVERS THE FULL TARGET, stable.** From the run's own listing:

| call | delivered [m3/s] | target [m3/s] | wet area [m2] |
|------|------------------|---------------|---------------|
| 1    | 0.0650 | 0.0650 | 1043.8 |
| 500  | 0.0650 | 0.0650 | 1052.2 |
| 1000 | 0.0650 | 0.0650 | 1011.8 |
| 1500 | 0.0650 | 0.0650 | 910.8 |
| ...  | 0.0650 | 0.0650 | (still full at iteration 8000 / T = 510 s) |

Exact to machine precision and **sustained** - still delivering the full target
8000 iterations in - with **zero limit trespasses**. Note the wet area
*shrinks* (1044 → 911 m2) as the reach drains its pre-wet surplus, and the
delivery still holds - the rate scales up to compensate
(0.065/911 ≈ **0.071 mm/s**, still 14x below the `max_rate` ceiling and applied
only where h > `min_depth`). The ceiling would only bind if the wet area fell
below ~65 m2, i.e. to 7 % of its current value.

This also confirms the earlier analytical estimate (wet taper-weighted area
~1054 m2) against the measured 1044-1052 m2.

**...but then the delivery decays.** Watching further, the wet area collapses and
the delivery follows it down:

| call | delivered [m3/s] | wet area [m2] |
|------|------------------|---------------|
| 1 | 0.0650 | 1044 |
| 20000 | 0.0650 | 258 |
| 38000 | 0.0650 | 67 |
| 38500 | 0.0646 | 64 | ← the `max_rate` ceiling starts binding |
| 46000 | 0.0407 | 29 |
| 58000 | **0.0172** | **8.0** |

The break at ~64 m2 is exactly where `0.065 / wet_area` crosses the 1 mm/s
`max_rate` ceiling, as predicted.

**Why the wet area collapses - the key physical finding.** Measured on the **T1
converged result**, i.e. with *no extraction whatsoever*:

| state | median depth in patch | % above 5 cm | taper-weighted wet area |
|-------|----------------------|--------------|-------------------------|
| pre-wet initial condition | 0.0712 m | 52.9 % | ~1079 m2 |
| **T1 converged, no extraction** | **0.0002 m** | **6.9 %** | **~88 m2** |

At Q = 2.4 m3/s the water surface does not cover the gravel bar - **and it is not
supposed to.** That is the entire point of this setup: the bar is **dry on top**
while 0.065 m3/s percolates **through it, below the surface**, within the ~0.5 m
porous depth recorded in `percolation-zone.gpkg`. The 53 % wetted patch at the
start was pre-wet surplus draining away - an artefact of the initial condition,
not the physical state. A dry bar in the converged run is the model behaving
correctly.

**So taking the withdrawal from the patch is conceptually wrong.** There is no
surface water on the bar to take. The water leaves the surface where it *enters
the gravel* - at the upstream edge of the bar, which is exactly where the
`int-outflow-lose` line is drawn - travels through the gravel, and resurfaces at
`int-inflow-gain`. T5's collapsing wet area is the model correctly reporting that
the bar has no surface water; the routine was simply asked to take water from the
wrong place.

**Final result: ran the full 4000 s and CONVERGED to -0.137 %**, ending at
0.0075 m3/s delivered with 2.05 m2 of wet area. Side channel: **29.3 % wetted,
mean depth 0.0157 m** - i.e. indistinguishable from the no-exchange baseline.

**Verdict: ⚠️ numerically sound, physically misplaced.** The sink belongs in the
channel at the losing line, not on the dry bar surface. → T6.

### Side-channel comparison (the deliverable measurement)

Wetted fraction and mean depth in a 40 m neighbourhood of the gaining line, all
from converged 4000 s runs:

| rung | losing region | delivery at steady state | wetted | mean depth |
|------|---------------|--------------------------|--------|------------|
| T1 | none (control) | - | 29.8 % | 0.0160 m |
| T4 | patch, uniform | decayed | 29.6 % | 0.0158 m |
| T5 | patch, wet-area | 0.0075 m3/s (12 %) | 29.3 % | 0.0157 m |
| **T6** | **12 m line strip** | **0.0650 m3/s (100 %)** | **33.9 %** | **0.0219 m** |

Every patch-based route is stable and converged - and **none of them changes the
side channel**. That is the measurement that matters: a run can look perfect on
every numerical criterion (T4 converged to -0.757 %, T5 to -0.137 %) and still do
nothing for the physics you care about.

**T6 is the only configuration that feeds the side channel:** +4.1 percentage
points of wetted area (+14 % relative), **+37 % mean depth** (0.0219 vs 0.0160 m),
and +22 % on the wet-node mean (0.0631 vs 0.0516 m). The domain also holds more
water (3043 m3 vs T1's 2910 m3), consistent with the extra volume sitting in the
side channel.

---

### Attempt T6 - 2026-08-02 - USER_RAIN over a 12 m strip along the losing LINE

**Hypothesis:** the withdrawal belongs where surface water actually **enters the
gravel** - the channel at the losing line - not on the dry bar it then flows
beneath. A strip along the line lies in the wetted channel even at steady state,
so it can supply the full 0.065 m3/s indefinitely.

**This is the physically faithful arrangement**, not a workaround (see
[the physical model](#what-is-actually-being-modelled)): surface water leaves the
channel at the losing line, travels through the gravel below the dry bar, and
resurfaces at the gaining line.

**Evidence (measured on the T1 converged result, no extraction):**

| region around the losing line | wet area | rate needed for 0.065 m3/s |
|-------------------------------|----------|----------------------------|
| whole percolation patch | 88 m2 | 0.74 mm/s (marginal, and shrinking) |
| 3 m strip | 42 m2 | 1.54 mm/s - **exceeds `max_rate`** |
| 6 m strip | 89 m2 | 0.73 mm/s |
| **12 m strip** | **183 m2** | **0.36 mm/s** ✅ |
| 20 m strip | 280 m2 | 0.23 mm/s |

Along the line the median depth is ~0.07 m with ~55 % of nodes above 5 cm - a
completely different regime from the bar (0.2 mm / 6.9 %).

**Setup:** `percolation.mode: fortran`, **`percolation.losing_region: line`**,
`boundaries.internal_source_region_width: 12.0`; 8 cores.

**Command:** `python cases/isar-2025/ladder.py T6 --ncsize 8`

**Result: ✅ SOLVED - converged to -0.167 % over the full 4000 s while delivering
the full 0.065 m3/s, and the side channel is measurably wetter.** Built regions:
losing 368 m2 (1790 nodes), gaining 544 m2 (2587 nodes). From the run's own
listing:

| T [s] | delivered [m3/s] | wet area [m2] |
|-------|------------------|---------------|
| 21.7   | 0.0650 | 266.3 |
| 519.5  | 0.0650 | 180.8 |
| 1022.3 | **0.0650** | **178.7** |

The wet area **plateaus** at ~179-181 m2 - **within 1-2 % of the 183 m2 predicted**
from the T1 converged field - and the delivery holds at the full target (0.36 mm/s
needed, well inside `max_rate`). Compare T5 over the same span: 2.2 → 2.05 m2 wet,
0.008 m3/s. That contrast between a **plateauing** and a **collapsing** wet area is
the whole story.

**Why this one holds and the patch does not:** the line strip lies in the **flowing
channel**, so water withdrawn there is replenished by the through-flow. The gravel
bar is hydraulically isolated at this discharge - whatever is taken from it is not
replaced, which is why extraction dragged it from T1's 88 m2 down to ~2 m2.

**Verdict: ✅ THIS IS THE CONFIGURATION TO USE.**

```yaml
percolation:
  zone: user-sources/geodata/percolation-zone.gpkg
  mode: fortran
  losing_region: line       # the bar is dry ON TOP by design; the water goes UNDER it
boundaries:
  internal_source_region_width: 12.0
```

**Remaining approximations** (inherent to representing subsurface flow in a 2D
surface model - none of them blocks the steady run):

* **No travel time.** The routine reinjects in the *same* time step. Real
  percolation through ~110 m of gravel takes hours. Irrelevant for a steady run;
  it would matter for the unsteady/hydrograph case, where a lag (a small storage
  reservoir in `USER_RAIN`) would be needed.
* **The exchange is prescribed, not computed.** 0.065 m3/s is imposed rather than
  derived from a head gradient. A Darcy cross-check against the recorded 0.5 m
  porous depth and the bar cross-section would test whether that rate is
  consistent with a plausible gravel conductivity - worth doing, but it needs a
  conductivity estimate that the model cannot supply.
* **The withdrawal is spread over a 12 m strip** rather than concentrated at the
  true infiltration face, because a narrower strip cannot supply the rate without
  exceeding `max_rate` (3 m needs 1.54 mm/s). This smears the loss slightly
  upstream/downstream of the actual entry point.

**Note on the diagnostic itself:** the first version of the print guarded on
`LT`/`LISPRD`/`IPID` and **never fired** - those counters do not line up inside
`USER_RAIN` the way they do in TELEMAC's own output code. It now throttles on the
routine's own `SAVE`d call counter, which is why the label reads `CALL n` rather
than an iteration number.

---

## Revision R1 - 2026-08-03 - too much water, too fast an outlet, and the campaign discharge

Feedback on the 2026-08-02 results: **too much water in the model** - many small
alcoves and side channels, *including ones on the floodplain*, came out wetted that
should be dry, and running longer did not help - plus **far too high a velocity
across the `outflow` line**. Three fixes were made, one measurement was added, and
one thing was found that neither fix accounts for.

### R1.1 The wrongly wetted alcoves: the pre-wet seeded water that cannot drain

This one is exactly as diagnosed by eye, and the "running longer would not fix it"
part is the key to it.

`initialization.prewet_depth: 0.30` seeded a smoothed longitudinal water surface
0.30 m over the thalweg across **every node of the `channel` mesh zone**. That zone
polygon also covers alcoves, abandoned side channels and floodplain hollows that at
2.4 m3/s sit *above* the water surface. Seeding them fills bowls whose rim is higher
than the water level around them - and **TELEMAC-2D has neither infiltration nor
evaporation, so water in a closed depression has no way out at all**. It is still
there at t = 4000 s, and it would still be there at t = 40000 s. A longer run cannot
repair an initial condition of this kind.

Measured on the 2026-08-02 result (`prescribed-q`):

| | area | volume |
|---|---|---|
| wetted at t = 4000 s | 14 411 m2 | 2 992.7 m3 |
| of that, standing still at < 2 cm/s | **4 899 m2 (34 % of the wet area)** | **322 m3 (10.8 % of the volume)** |
| of that stagnant area, seeded by the pre-wet | 4 491 m2 (92 %) | 309 m3 |

**The fix** (`axqua.steering.spill_elevations`, new
`initialization.drainable_prewet`, on by default) computes each node's **spill
elevation** - the lowest level at which it is still connected to the outflow - with
a priority-flood sweep over the mesh graph (Barnes et al.), and seeds water **only
where that spill elevation is the node's own bed**, i.e. only on ground that drains
freely. Everything behind a rim is left dry. 0.6 s of CPU on this 231 k-node mesh.

An intermediate rule was tried and rejected, and it is worth recording why: seeding
wherever the node is *connected at the seeded level* (`spill <= surface`) sounds
right but is not, because such a column only drains **down to the rim** and the rest
stays. That residue works out at **313.7 m3** on this mesh - which is essentially
the 322 m3 of stagnant water actually observed. Under-seeding a genuine channel pool
costs nothing by comparison: the flow refills it within seconds.

Effect on the seed: **14 331 of 84 087 seeded nodes dropped, 25.5 % of the seeded
depth-sum**; the remaining trapped depth-sum is 48 m spread over the whole mesh
(bounded by `initialization.drainable_tolerance`, 0.02 m per node).

### R1.2 The outlet velocity: the prescribed stage was 0.24 m too low

The outflow rating was synthesised by the **`trapezoid`** method: normal depth in a
rectangle as wide as the outflow line (9.55 m). The real section is V-shaped, so a
rectangle at the same stage has far more flow area than the ground does - the method
therefore returns a stage that is **too low**. It gave `WSE 815.1139`, and the
boundary then held the outlet *below* the level the reach itself wants:

| | |
|---|---|
| prescribed WSE | 815.114 m |
| mean depth at the outlet | 0.189 m |
| **mean \|U\|** | **1.03 m/s** |
| **peak \|U\|** | **2.22 m/s** |
| Froude number | **~1.4 - supercritical** |

The flow had to accelerate through a ~1.3 m2 gap to pass 2.4 m3/s. That is a
boundary artefact, not reach hydraulics.

**The fix** is a new rating method, `boundaries.rating_method: section`
(`axqua.rating.section_rating` /`synthesize_outflow_rating_from_section`): the
**actual DEM cross-section** along the outflow line is integrated for wetted area and
perimeter, each sample carries the `ks` of the roughness zone it falls in, and the
Keulegan fully-rough log law `C = 18 log10(12 R / ks)` with `Q = C A sqrt(R S)` is
inverted for the stage by bisection. At Q = 2.4 m3/s it gives **WSE 815.347** -
0.23 m higher, and the section then conveys the discharge at a subcritical depth.

The rating is no longer read from `user-sources/geodata/rating-curve.csv` (that file
is left untouched, and is the stale trapezoid one); each scenario now synthesises its
own into `<scenario>/preprocessing/rating-curve.csv`.

### R1.3 Roughness: zone 4 lowered as requested

`user-sources/geodata/roughness-table.csv`, zone 4 (`Gravel (coarse)`, 9 281 m2):
**ks 0.11 -> 0.05 m** (backup kept as `roughness-table.csv.bak`). Note this is a
change in the *opposite* direction from the depth mismatch below - a smaller ks makes
the model shallower and faster, not deeper - so it works against matching the
measured depths. Both values are defensible for coarse gravel (`ks ~ 1-3 d90`); 0.11
sits at `~2 d90`, 0.05 at `~1 d90`.

### R1.4 Discharge across the threads: `baffle-XS-q.csv`

New module `axqua.sections`, wired into `initial_run.py`, integrates

```
Q = ∫ (H·U, H·V) · n ds
```

along each line of `baffles.gpkg` (EPSG:4326, reprojected on read), interpolating
`H·U` and `H·V` with the same linear P1 basis TELEMAC uses, at a quarter of the local
mesh edge length. Written to `<scenario>/simulation/baffle-XS-q.csv` with the two
columns `Baffle Name` and `discharge (m3/s)`; the console additionally shows wetted
width, mean depth and mean velocity per section.

It validates against the prescribed boundary conditions: on the 2026-08-02 result
`righ US` read **0.8038** against the prescribed 0.800 m3/s inflow RB (+0.5 %),
`left US` **1.6030** against 1.600 (+0.2 %), and `left Side` **0.0633** against the
0.0650 m3/s percolation injection.

### R1.5 The finding neither fix accounts for: the campaign discharge is ~5.35 m3/s

The September-2025 FlowTracker campaign supplied as ground truth
(`FT_TKE_Summary_Sep25.xlsx`, `Isar` tab) contains **two complete cross-sections**.
Integrating them with the mid-section method (`check_ground_truth.py`):

| transect | verticals | width | area | Q(all pts) | Q(0.6 depth) | Q(0.2/0.8) |
|---|---|---|---|---|---|---|
| `ft_deadwood` (upstream) | 27 | 13.0 m | 4.80 m2 | **5.379** | 5.415 | 5.361 |
| `ft_dswood` (downstream) | 30 | 14.5 m | 5.63 m2 | **5.330** | 5.314 | 5.310 |
| `ft_willows_leavs` | 15 | 7.0 m | 3.39 m2 | 3.383 | 3.472 | 3.339 |
| `ft_leaves` | 8 | 3.5 m | 1.63 m2 | 1.322 | 1.348 | 1.309 |

The two full sections are ~300 m apart and agree to **0.6 %**, and the three standard
depth-averaging conventions agree to **1 %**. That is about as strong as an internal
consistency check on a field campaign gets.

**The reach was carrying ~5.35 m3/s during the campaign - the model is set up for
2.4 m3/s.** This, not roughness and not the initial condition, is the dominant reason
the model comes out shallower and slower than the measurements at every transect. No
roughness value closes a factor-2.2 discharge gap.

This is left as it is, because `boundaries.prescribed_flowrate: 2.4` is a modelling
decision, not a bug to fix silently. If the intent is to reproduce the campaign, it
is a one-line change (with the 1.6 / 0.8 split scaled to 3.57 / 1.78), and the
outflow rating regenerates automatically from the section method at the new Q.

### R1.6 Results - both scenarios re-run with all three fixes

Rebuilt (`preprocessing.py`) and re-run (`initial_run.py`) for both scenarios,
4000 s each, 16 MPI processes. Both reached a **flat boundary-flux balance**:
domain volume constant to 0.1 m3 over the last 500 s, relative volume error at
machine precision (~1e-15) throughout.

| | prescribed-q **before** | prescribed-q **after** | green-ampt **after** |
|---|---|---|---|
| inflow / outflow (m3/s) | 2.400 / -2.405 | 2.400 / **-2.405** | 2.400 / **-2.403** |
| domain volume (m3) | 2 992.6 | 2 833.8 | 2 780.9 |
| wetted area | 14 411 m2 (21.8 %) | **13 593 m2 (20.6 %)** | 13 593 m2 (20.6 %) |
| stagnant (< 2 cm/s) | 4 899 m2 / 322 m3 | 4 122 m2 / 237 m3 | 3 969 m2 / 206 m3 |
| ... of which pre-wet-seeded | 4 491 m2 / **308.6 m3** | 3 155 m2 / **97.9 m3** | 3 089 m2 / 94.9 m3 |
| isolated puddles (no path to a boundary) | - | **749 m2 / 29.7 m3 (1.0 % of volume)** | 990 m2 / 37.8 m3 (1.4 %) |
| **outlet mean depth** | 0.189 m | **0.262 m** | 0.262 m |
| **outlet mean \|U\|** | **1.03 m/s** | **0.695 m/s** | 0.698 m/s |
| **outlet peak \|U\|** | **2.22 m/s** | **1.23 m/s** | 1.24 m/s |
| **outlet Froude** | **~1.4 (supercritical)** | **0.43 (subcritical)** | 0.43 |
| domain peak \|U\| | 2.22 m/s | 1.70 m/s | 1.75 m/s |

The trapped pre-wet water is down **68 %** (308.6 -> 97.9 m3) and what is left is
mostly not trapped at all: only **29.7 m3 (1.0 % of the domain volume)** now sits in
wet patches with no path to a boundary. The remaining slow water is in channel pools
and backwaters that are connected and flowing - 890 m3 of the volume is in closed
depressions, but only 133 m3 of that is stagnant, i.e. these are real pools kept
filled by the flow, not seeded ponds.

The outlet is fixed outright: **the drawdown is gone** and the outflow section is
subcritical at a plausible 0.7 m/s.

#### Cross-section discharges (`baffle-XS-q.csv`)

| Baffle Name | prescribed-q | green-ampt | measured (Sep-2025) | model / measured |
|---|---|---|---|---|
| `righ US` | 0.8028 | 0.8023 | - (prescribed 0.800) | +0.4 % vs the BC |
| `left US` | 1.6026 | 1.5988 | - (prescribed 1.600) | +0.2 % vs the BC |
| `righ US` + `left US` | 2.4053 | 2.4011 | 5.379 (`ft_deadwood`) | **0.447** |
| `right-main` | 2.3351 | 2.2980 | 4.705 (`ft_willows_leavs`+`ft_leaves`, partial) | 0.496 |
| `left Side` | 0.0610 | 0.0830 | 0.150 (`ft_sidech_rb`, 106 m upstream) | 0.407 |
| `downstream` | 2.4020 | 2.4039 | 5.330 (`ft_dswood`) | **0.451** |

Two independent checks fall out of this table:

* **The extraction is right.** The two upstream baffles return the prescribed inflow
  boundary conditions to within 0.4 %, and `downstream` returns the total to 0.08 %.
* **The flow split is right; only the total is wrong.** Both *complete* measured
  sections give a model/measured ratio of **0.451 and 0.447** - against the ratio of
  the prescribed to the measured discharge, **2.400 / 5.35 = 0.449**. The model
  distributes water between the threads of the braid essentially exactly as measured;
  it simply routes 45 % of the water that was there. `left Side` is the one that does
  not follow (0.407), and should not: it is fed by the *fixed* 0.065 m3/s percolation
  exchange, which does not scale with the reach discharge.

#### Against the measured verticals

| transect | GT depth | model | GT \|U\| | model |
|---|---|---|---|---|
| `ft_deadwood` (27 verticals) | 0.358 | 0.276 | 0.986 | 0.443 |
| `ft_dswood` (30) | 0.380 | 0.211 | 0.781 | 0.451 |
| `ft_leaves` (8) | 0.454 | 0.186 | 0.788 | 0.313 |
| `ft_willows_leavs` (15) | 0.490 | 0.384 | 0.985 | 0.645 |

Still shallow and slow, by about the amount the discharge deficit predicts - see
[R1.5](#r15-the-finding-neither-fix-accounts-for-the-campaign-discharge-is-535-m3s).
**Do not calibrate roughness against these numbers at Q = 2.4 m3/s**: the optimiser
would drive `ks` to absurd values trying to make 2.4 m3/s look like 5.35 m3/s.

#### Percolation exchange

`prescribed-q` delivered the full 0.0650 m3/s throughout (`left Side` = 0.0610 at the
baffle, the rest still in transit through the side channel). `green-ampt` at
`k_f = 3e-4 m/s` now delivers more than before, because the higher outflow stage
backs water up and the Green-Ampt rate `f = k_f (h + Lz + hf) / Lz` grows with depth:
`left Side` reads 0.0830 m3/s. **The `k_f` calibrated to reproduce 0.065 m3/s is
therefore no longer 1.9e-4 m/s** (the pre-R1 estimate); re-derive it from this run
before using scenario B quantitatively.

#### Note

`pythomac` is still not importable in `axqua-env`, so the four
flux-convergence CSV/PNG files were not written (`convergence analysis skipped:
ModuleNotFoundError`). Convergence was read off the `.sortie` directly instead
(volume flat, outflow -2.405 against the 2.400 inflow). `pip install pythomac`
would restore the plots.


### R1.7 Re-run at DURATION 5000 s - convergence at the hydraulic tolerance

At 4000 s the relative flux imbalance was still 2.07e-3 / 1.17e-3. That residual was
checked and is **not solver error**: over the last 20 printouts ``dV/dt`` equals
``Q_in - Q_out`` to ~1e-5 m3/s, i.e. mass closes and the domain is simply still
draining the last of the pre-wet excess, at ~0.2% of the throughflow. The imbalance
decays exponentially (e-folding ~1300 s / ~1100 s), so `hydrodynamics.duration` was
raised to **5000 s** and both scenarios rebuilt and re-run.

The tolerance itself was also made explicit (`hydrodynamics.flux_tolerance`, default
**1e-3**): the imbalance is a *relative* measure, so discharge, depth and velocity
inherit it directly, and 0.1% is far inside field-measurement uncertainty and inside
the 5% the mesh-convergence study accepts. **1e-4 is reserved for a hotstart seed**,
where a residual transient is inherited by every perturbed calibration run rather
than averaged out. The absolute steady criterion is now `flux_tolerance x
prescribed_flowrate` instead of a fixed 1e-3 m3/s, so the hotstart gate means the
same thing on a 2.4 m3/s side channel as on a 200 m3/s river.

| | prescribed-q | green-ampt |
|---|---|---|
| final relative imbalance | 1.01e-03 | **4.99e-04** |
| converged at 1e-3? | **no** - 1% short | **yes, from 4535 s** (iteration 56000) |
| `hotstart2d.cas` written? | no | **yes** |
| residual drainage | 0.147% of Q | 0.059% of Q |
| mass closure (dV/dt vs Q_in-Q_out) | 4.3e-05 m3/s | 1.4e-04 m3/s |
| domain volume | 2 781.2 m3 | 2 800.7 m3 |
| wetted area | 13 456 m2 (20.4%) | 13 600 m2 (20.6%) |
| outlet mean depth / \|U\| / peak \|U\| | 0.253 m / 0.671 / 1.240 m/s | same section |
| projected time to 1e-3 | 5152 s | reached |
| projected time to 1e-4 | 8737 s | 7110 s |

`green-ampt` converged and has its hotstart; `prescribed-q` ended at 1.01e-3, i.e.
**1% above the threshold** - its decay slowed slightly (e-folding 1332 -> 1557 s) so
it needs ~5200 s rather than the ~4950 s projected from the 4000 s run. The
difference has no physical consequence (a 0.147% drainage rate), but it does mean no
`hotstart2d.cas` was generated for that scenario: **raise `duration` to ~5500 s if
prescribed-q is to seed a calibration fleet.**

#### Cross-section discharges at 5000 s

| Baffle Name | prescribed-q | green-ampt | measured (Sep-2025) |
|---|---|---|---|
| `righ US` | 0.8028 | 0.8036 | (prescribed 0.800) |
| `left US` | 1.6030 | 1.6005 | (prescribed 1.600) |
| `right-main` | 2.3354 | 2.3086 | 4.705 (partial section) |
| `left Side` | 0.0625 | 0.0811 | 0.150 |
| `downstream` | 2.4072 | 2.4034 | 5.330 |

The model/measured ratio at the two complete sections is 0.452 (`downstream`) and
0.447 (`righ US`+`left US`) against the discharge ratio 2.400/5.35 = **0.449** - the
flow split between the braid threads is reproduced essentially exactly, and the total
is the only first-order discrepancy (see R1.5).

---

---

## Revision R2 - 2026-08-04 - the wetted extent: where the water actually is

**User report on the 5000 s results.** Three observations: too many wetted patches
toward the banks ("might be an artifact of too high flooding during model
initialization"); the polygon-bounded percolation zone showing surface water where
the bar should be dry; and apparently flooded gravel bars at the outlet ("a little
backwater ... outlet water depth is too high?").

Two of the three trace to one cause. The third turns out to be a different
phenomenon than suspected, and the measurement contradicts the backwater
hypothesis - reported here rather than quietly "fixed".

All figures below are measured on the **green-ampt** 5000 s result; `prescribed-q`
is within 3 % on every one of them.

### R2.1 The cause: the pre-wet seeds a surface above the one the run converges to

`steering._longitudinal_prewet_depth` built the seeded water surface as *"25th
percentile of the bed per 5 m bin + `prewet_depth` (0.30 m)"*. On this braided reach
that percentile sits well above the thalweg, because each bin spans both threads,
the island and the bars between them. Comparing the seeded surface with the surface
the run converges to, per 20 m of reach:

| | seeded vs converged surface |
|---|---|
| median | **+0.28 m** |
| range along the reach | +0.10 … +0.60 m |

So the seed floods bar tops and bank shelves. What happens next is the decisive
point: **that water cannot leave.** TELEMAC-2D has neither infiltration nor
evaporation, so the excess drains only where it can flow; on flat ground over ks
0.05-0.5 m gravel it stops as a 1-5 cm film with velocities of order 5 mm/s.

| at t = 5000 s | area | volume |
|---|---|---|
| seeded by the pre-wet | 14 958 m2 | 4 815 m3 |
| wetted | 13 600 m2 | 2 789 m3 |
| **actively flowing** (H > 0.05 m, \|U\| > 0.15 m/s, connected) | **7 101 m2** | **2 182 m3** |
| **stagnant film** (\|U\| < 0.05 m/s) | **4 694 m2 (35 % of wetted)** | 341 m3 |
| of that film, seeded rather than flow-spread | **75 %** | |
| isolated puddles (not connected to the main body) | 986 m2, 690 patches | 37 m3 |

And the film has **stopped draining**, which is what makes it a modelling error
rather than an unfinished transient:

| t [s] | 390 | 923 | 1907 | 2879 | 3849 | 5000 |
|---|---|---|---|---|---|---|
| film area [m2] | 4 118 | 4 614 | 4 779 | 4 756 | 4 723 | **4 694** |

-1.8 % over the last 3 100 s. Extrapolated, it would need ~10^5 s. **The user's
assessment that "even if the simulations ran longer, that issue could not be
addressed" is correct.**

Note that `drainable_prewet` (R1.1) was already active and working: it removes water
seeded *behind a rim*. It does not touch water perched on ground that drains freely
but conveys nothing - which is the bulk of this.

### R2.2 The percolation zone: the same cause, plus nothing to remove it

Of the 2 188 m2 patch, 807 m2 was wetted. Splitting that against the local converged
water surface:

| | area | volume | mean \|U\| |
|---|---|---|---|
| below the local water surface (the channel-connected toe - legitimate) | 186 m2 | 15.2 m3 | 0.018 m/s |
| **perched above it** (88 % of it seeded) | **620 m2** | 17.6 m3 | 0.007 m/s |

With `losing_region: line` the USER_RAIN sink draws from the 12 m strip along the
losing line - correct physics (R1/T6) - but that means **nothing withdraws water
standing on the bar itself**.

### R2.3 The outlet: measured as a drawdown, not backwater

The bars wetted near the outflow line are not flooded by the boundary:

| within 10 m of the outflow line | |
|---|---|
| wetted area | 171 m2 |
| of it seeded by the pre-wet | **170 m2 (99 %)** |
| highest wetted bed there | 815.84 m |
| prescribed outflow stage | **815.3473 m** |

Those bar tops stand up to **0.5 m above** the prescribed stage, so the boundary
cannot be wetting them - it is the same seed film.

The free-surface profile of the *actively flowing* water says the same thing from
the other direction:

| distance from the outflow line | 0-3 m | 3-6 m | 6-10 m | 10-20 m | 20-40 m | 40-70 m |
|---|---|---|---|---|---|---|
| free surface [m] | 815.370 | 815.405 | 815.436 | 815.483 | 815.638 | 815.853 |
| depth [m] | 0.321 | 0.330 | 0.322 | 0.291 | 0.195 | 0.221 |
| Froude | 0.48 | 0.45 | 0.42 | 0.41 | 0.64 | 0.42 |

The surface **falls into the boundary at 11.6 permille** against 7.2 permille in the
reach above: a mild drawdown (M2), the opposite of backwater. The larger depth right
at the outlet (0.32 m against 0.19 m at 30 m upstream) is the section narrowing from
~19 m of wetted width to the 9.55 m outflow line - the flow *area* is nearly
unchanged, 2.5 against 2.6 m2.

So the prescribed stage is at or slightly *below* the reach's own level, not above
it. Rather than adjust it against the evidence, the user asked for a **free (Neumann)
outflow variant** to settle the boundary question independently of the rating; that
is scenario C below.

### R2.4 The fix: seed the normal-depth stage of the real cross-sections

Instead of a bed percentile plus a guessed depth, cut a real cross-section every
20 m along the centerline and seed the **stage that conveys the case discharge**
through it - the same Keulegan conveyance inversion (`C = 18 log10(12R/ks)`,
`Q = C A sqrt(R S)`) that already builds the outflow rating, factored out of
`rating.section_rating` as `rating.stage_for_discharge`. The local bed slope comes
from the gradient of the transect thalwegs, not one slope for the whole reach (the
local gradient here ranges 0.002-0.009, a factor of four).

A first attempt built each "section" from the scatter of mesh nodes in a
longitudinal bin. That is wrong and worth recording: sorting a point cloud by
cross-channel offset makes the bed jump between neighbouring samples, the wetted
perimeter comes out hugely too large, the hydraulic radius too small, and the
required stage lands **+0.88 m** too high. Real perpendicular transects, sampled at
0.5 m, fix it.

The seeded surface is then `thalweg + fill x (normal stage - thalweg)`, with
`prewet_fill` **deliberately below 1**: under-seeding is recoverable (the flow
refills a pool in seconds), over-seeding is not. Calibrated against the converged
surface:

| `prewet_fill` | median error | p90 | seeded area | seeded volume |
|---|---|---|---|---|
| 1.00 | +0.133 m | +0.367 | 12 206 m2 | 4 194 m3 |
| 0.80 | +0.027 m | +0.257 | 9 485 m2 | 2 914 m3 |
| **0.70** | **-0.030 m** | **+0.205** | **8 294 m2** | **2 384 m3** |
| 0.60 | -0.082 m | +0.152 | 6 957 m2 | 1 917 m3 |
| *old seed* | *+0.28 m* | *+0.59* | *14 958 m2* | *4 815 m3* |

0.70 seeds essentially the active flow area (7 101 m2) and slightly under-fills the
converged volume - a 420 m3 gap, about 175 s of inflow.

Two further guards: `prewet_min_depth` (0.05 m) leaves a node dry rather than laying
down a feathered margin - a seed film is exactly what stalls - and the existing
`drainable_prewet` spill filter still runs on top.

Applied to the real mesh (before any solver run), against the old seed:

| | new seed | old seed |
|---|---|---|
| seeded area / volume | 6 207 m2 / 1 674 m3 | 14 958 m2 / 4 815 m3 |
| seeded on ground **above** the converged surface | **749 m2 / 89 m3** | 6 052 m2 / 933 m3 |
| seeded surface vs converged (active nodes) | median **-0.004 m** (p10 -0.196, p90 +0.259) | +0.28 m |
| seeded inside the percolation patch | 305 m2 | 1 135 m2 |
| seeded within 10 m of the outflow | 139 m2 | 208 m2 |

The area seeded above the converged surface drops by **88 %**.

### R2.5 The fix for the patch: drain it (`percolation.patch_drain`)

The bar is porous gravel: water lying on top infiltrates rather than ponding, so
surface water there is an artefact of a 2D model with no subsurface. `patch_drain`
adds the patch polygon as an **extra losing region** in USER_RAIN. It is emitted
**last**, and each node joins the first region containing it, so every node of the
prescribed losing and gaining strips keeps its own region - the calibrated
0.065 m3/s is not double-counted. The drain uses the Green-Ampt rate where a
conductivity is configured (green-ampt) and a capped drawdown otherwise
(prescribed-q), is limited by the same `min_depth` / `taper_depth` taper and the
half-the-available-water-per-step cap, so it can never dry a cell, and whatever it
extracts is added to what the gaining line reinjects - the routine stays mass-exact.

All four generated variants (prescribed / Green-Ampt x with / without the drain) are
compile-checked against the real TELEMAC modules in `tests/test_source_regions.py`.

### R2.6 Measuring it: `axqua/wetting.py`

The numbers above came from one-off scripts; they are now a module, so "is there
still too much water?" is a number rather than an impression, and successive runs
compare directly. `initial_run.py` writes both files next to the results:

* `wetting-report.csv` - wet / active / film / isolated-puddle area and volume, how
  much of each the seed put there, and the film trend over the last frames (still
  draining vs plateaued);
* `outlet-profile.csv` - banded free surface, depth, Froude and surface slope
  approaching the outflow, with a `backwater` / `drawdown` / `neutral` verdict.

Re-running the module on the *old* result reproduces every figure in R2.1-R2.3
exactly, which is how it was validated.

### R2.8 A leaner seed exposed a latent trap: the inflow plug is now mandatory

The first run with the new seed **aborted at t = 0**:

```
DEBIMP: PROBLEM ON BOUNDARY NUMBER      1
        GIVE A VELOCITY PROFILE ... OR CHECK THE WATER DEPTHS.
```

TELEMAC's `DEBIMP` distributes a prescribed discharge over the inflow section by
scaling a velocity profile with `Q/Q1`, where `Q1` is proportional to the integral of
`H` along that section. A dry inflow gives `Q1 = 0` and the run stops - the same trap
that rules out a `ZERO DEPTH` cold start, and the reason
`write_dry_start_conditions` lays down an inflow plug.

The old over-seeding hid it: a surface 0.28 m too high covered the inflow section by
accident. The normal-depth seed is by construction **shallowest at the upstream end**
(that is where the reach is highest), and the 0.05 m floor plus the drainable filter
then emptied it completely - **538 nodes** on the inflow cross-section left dry.

The fix is a guard rather than a tuning value: `write_initial_conditions` now
re-imposes the *same* inflow plug as the dry start (`_inflow_plug_mask`,
`dry_start_extent` / `dry_start_depth`) **after every other filter**, since none of
those filters has any reason to keep that section wet. It costs 538 nodes,
113 m2 and 57 m3 - 3 % of the seeded volume, all of it inside the inflow cross
section, where the flow is fastest and no film can form.

| green-ampt seed | area | volume |
|---|---|---|
| before the plug guard (run aborted) | 6 173 m2 | 1 662 m3 |
| with the plug | 6 286 m2 | 1 719 m3 |
| *old constant seed* | *14 958 m2* | *4 815 m3* |

Covered by `tests/test_prewet.py::test_prewet_always_wets_the_inflow_section`, which
sets a floor high enough to wipe out the whole seed and asserts the inflow section
still comes out wet.

### R2.7 Scenario C: free (Neumann) outflow

`case-config-freeoutflow.yml` is `case-config-greenampt.yml` with
`outflow_condition: free` and its own `scenarios/free-outflow/` tree - one setting
different, so the comparison isolates the downstream boundary. Caution recorded up
front: a 4 4 4 Neumann outlet is under-specified for subcritical flow (Fr ~ 0.45-0.5
here) and the level can drift or the reach drain progressively. If it does, that is
the result; the section rating stands.

### R2.9 Results - all three scenarios re-run at DURATION 5000 s

| | prescribed-q | | green-ampt | |
|---|---|---|---|---|
| | **before** | **after** | **before** | **after** |
| stagnant film \|U\|<0.05 m/s | 4 913 m2 | **2 886 m2** (-41 %) | 4 694 m2 | **2 694 m2** (-43 %) |
| of that film, seeded | 74 % | **37 %** | 75 % | **37 %** |
| isolated puddles | - | 211 patches / 235 m2 | 690 / 986 m2 | **181 / 212 m2** (-78 %) |
| wetted area | 13 456 m2 | 11 379 m2 (-15 %) | 13 600 m2 | 11 597 m2 (-15 %) |
| **actively flowing** | 6 999 m2 | **7 000 m2** | 7 101 m2 | **7 097 m2** |
| pre-wet seed | 14 907 m2 / 4 782 m3 | 6 304 / 1 736 | 14 958 / 4 815 | 6 314 / 1 734 |
| converged (rel. imbalance) | 1.01e-03 (never) | **1.25e-04 at 2 593 s** | 4.99e-04 at 4 535 s | **5.98e-05 at 2 478 s** |
| `hotstart2d.cas` | not written | **written** | written | **written** |
| wall time | ~4.2 h | **~1.5 h** | ~4.2 h | **~1.5 h** |

**The decisive line is the active area: 7 101 -> 7 097 m2 (green-ampt) and
6 999 -> 7 000 m2 (prescribed-q).** The flow field is untouched; what disappeared is
only water that was never carrying anything. Everything downstream of that agrees:
the baffle discharges hold to ~1 %, and the outlet profile is unchanged.

Convergence improved by an order of magnitude *and* halved the runtime, for the same
reason: the solver no longer spends the run draining a seed that was 2.8x too large.
`prescribed-q`, which previously never reached 1e-3 within 5000 s, now converges at
2 593 s and finally gets its `hotstart2d.cas` - so **both** scenarios can now seed a
HydroBayesCal fleet.

#### Cross-section discharges (`baffle-XS-q.csv`)

| Baffle Name | prescribed-q | green-ampt | (before, green-ampt) |
|---|---|---|---|
| `righ US` | 0.8033 | 0.8029 | 0.8036 |
| `left US` | 1.6004 | 1.6010 | 1.6005 |
| `right-main` | 2.3332 | 2.2925 | 2.3086 |
| `left Side` | 0.0629 | 0.0804 | 0.0811 |
| `downstream` | 2.3899 | 2.3968 | 2.4034 |

#### The percolation patch

| | before | after |
|---|---|---|
| wetted on the patch (green-ampt) | 807 m2 / 32.8 m3 | **487 m2 / 14.2 m3** |
| of that, **perched** above the local water surface | 620 m2 / 17.6 m3 | **353 m2 / 9.2 m3** |
| of the perched water, seeded | 88 % | **38 %** |
| toe genuinely below the local surface | 186 m2 | 135 m2 |

The **patch drain converged to 0.00398 m3/s** - it clears the standing water and then
tapers off. Total exchange delivered at the gaining line went 0.0993 -> 0.1027 m3/s,
i.e. **+3.4 %**: the drain does *not* end up as a second exchange path competing with
the calibrated one. (Mid-transient it peaks near 0.11 m3/s while the seeded water on
the bar is being cleared, which is worth knowing before reading an early printout.)

The residual 353 m2 is a floor set by the routine's own safety limiter: the
withdrawal tapers to zero as the depth approaches `min_depth` (0.05 m), so it cannot
by construction remove the last few centimetres - that taper is exactly what stops a
sink from drying a cell and collapsing the time step (see T2/T3).

#### The outlet: unchanged, and still a drawdown

| distance from the outflow line | 0-3 m | 3-6 m | 6-10 m | 20-40 m | 40-70 m |
|---|---|---|---|---|---|
| free surface [m] | 815.368 | 815.404 | 815.435 | 815.637 | 815.854 |
| depth [m] | 0.325 | 0.329 | 0.313 | 0.195 | 0.223 |
| Froude | 0.48 | 0.45 | 0.42 | 0.65 | 0.42 |

Near-boundary surface slope **11.8 permille against 7.3 permille** in the reach above -
the same mild drawdown as before, and the depth at the outlet is unchanged (0.321 ->
0.322 m). The gravel bars that appeared flooded near the outlet are now dry: they
were seed film standing up to 0.5 m above the prescribed stage, not backwater.

#### Scenario C (free outflow): the boundary is NOT optional

`case-config-freeoutflow.yml` built correctly - 29 outflow nodes coded `4 4 4`, all
`PRESCRIBED ELEVATIONS` placeholders - and TELEMAC **diverged after 13 s of simulated
time**:

```
LIMIT VALUES TRESPASSED, TELEMAC-2D IS STOPPED
```

(the `CONTROL OF LIMITS` guard stopping it cleanly; the diverged state held 48 022 m3
over 8 473 m2, i.e. a mean depth of 5.7 m). This is the expected result rather than a
setup error: the reach is **subcritical** at the outlet (Fr ~ 0.45-0.5), so it
physically *requires* downstream control, and a Neumann condition supplies none - the
problem is ill-posed. Relaxing `control_of_limits` would only postpone the blow-up.

So the free-outflow test settles the boundary question from the opposite direction:
far from the prescribed stage backing water up, **some** stage prescription is
mandatory here, and the section rating's 815.3473 m produces a mild drawdown rather
than backwater. The scenario is kept for the record; the section rating stands.

---

---

## Revision R3 - 2026-08-06 - the last of the wrongly wetted patches, and the pool

**User report on the R2 results.** Two things, both correctly diagnosed by the user:
*"very small water depth (<1cm) and very small velocity means (<0.005 m/s) are
responsible for some of the seemingly wrongly wetted patches"*, and *"within the
percolation polygon is a pool in the DEM which should fill with water - maybe by
enabling the below surface water table up to 0.2 m under the gravel surface?"*.
`free-outflow` is dropped from the run set (see R2.7); the work continues on
`prescribed-q` and `green-ampt`, with green-ampt preferred.

### R3.1 The film is real, and it is almost pure area

Measured on the R2 green-ampt result:

| depth band | area | volume | mean \|U\| |
|---|---|---|---|
| 0.0-0.5 cm | 1 098 m2 | 1.4 m3 | **0.0000 m/s** |
| 0.5-1.0 cm | 405 m2 | 3.0 m3 | 0.0047 m/s |
| 1-2 cm | 755 m2 | 11.4 m3 | 0.0155 m/s |
| 2-5 cm | 1 219 m2 | 40.1 m3 | 0.0442 m/s |
| > 10 cm | 8 344 m2 | 2 565 m3 | 0.4903 m/s |

Water thinner than 1 cm covers **1 503 m2 - 13 % of the wetted area - while holding
4.4 m3, 0.16 % of the volume**. Invisible in any budget, dominant in any picture. On
a bed with Nikuradse ks 0.05-0.5 m it is not flowing *over* the bed at all; it is
standing *within* the grain roughness, and in the field it drains into the substrate.

**TELEMAC cannot remove it.** The only candidate keywords do the opposite:
`H CLIPPING` + `MINIMUM VALUE OF DEPTH` raise H to a floor, which the dico states
"is equivalent to adding mass", and it marks the second "not fully implemented".
So the term is generated into the `USER_RAIN` routine the case already owns
(`drying.film_infiltration`): where the depth is below `film_depth` (1 cm) **and**
the speed below `film_velocity` (0.005 m/s), water is withdrawn at `film_rate`
(1e-5 m/s), capped by the same half-the-available-water-per-step limiter as the
percolation sink. The velocity gate is what keeps the term off the active wet/dry
margin, where the flow genuinely delivers water.

**Why the water is reinjected rather than lost.** A net domain sink S makes the
steady-state boundary budget read `|Q_in| - |Q_out| = S`. At 0.0044 m3/s against
2.4 m3/s that is 1.8e-3 - *above* the 1e-3 hydraulic tolerance - so the run would
never be reported as converged and no `hotstart2d.cas` would ever be written. The
film water therefore joins the percolation total and resurfaces at the gaining line,
which is the same statement the whole lose/gain construct already makes about water
entering the gravel. (This is also why `drying.film_infiltration` currently requires
`percolation.mode: fortran` - without a gaining line there is nowhere for it to go.)

Alongside it, `hydrodynamics.wet_depth` (0.01 m) makes the reporting threshold
explicit, so the number in `wetting-report.csv` and the filter used in ParaView are
the same documented figure.

### R3.2 The pool: the bar has a water table, and the model did not

Inside the percolation polygon the DEM holds **24 closed depressions, 147 m2 with
11.1 m3 of capacity**; the largest is **64.9 m2, rim 817.17 m, bed 816.84 m, 0.33 m
deep, 6.9 m3**. It was holding a 2.6 cm skim. Three separate mechanisms conspired:

* it cannot fill from the surface - it is a closed depression on a bar *above* the
  channel, so no flow path reaches it;
* `drainable_prewet` deliberately refuses to seed anything behind a rim (R1.1) -
  correctly, for water with no source;
* the R2 patch drain pulled it down to the `min_depth` taper.

All three are right for *unsupported* water. This pool has a source: it cuts below
the water table of the gravel bar.

**The water table is not a free parameter here.** The bar exchanges with the channel
at two known places - the losing line (measured WSE **817.318 m**) and the gaining
line (**816.807 m**) - and the surface joining them *is* the water table of that
through-flow; its gradient is what drives the exchange in the first place. Fitted as
a plane (`axqua/watertable.py`):

| | |
|---|---|
| gradient / fit residual | **7.65 permille** / 0.049 m |
| wetted inside the patch | **118 m2 / 10.4 m3**, max depth **0.35 m** (the pool, to its rim) |
| patch left dry | 2 070 m2 = **95 %**, median freeboard 0.37 m |
| outside the patch the same plane covers | 22 598 m2 of bed -> **clipped to the polygon** |

The user's suggested "0.2 m under the gravel surface" and this plane agree in effect:
the plane sits a median 0.37 m below the bar surface and surfaces only in the
depressions, which is what the suggestion was reaching for - but it is derived from
measured levels rather than assumed, and it slopes the right way.

A plane is five numbers, so the Fortran side needs no per-node array: the
coefficients are baked into the generated routine, where the drain's taper floor
becomes `MAX(min_depth, z_table - z_bed)`. The drain still clears standing water off
the bar top; it can no longer empty a pool that cuts below the saturated zone.

**The plane can be built without a prior run.** Read off the *seeded* normal-depth
surface at the two lines it gives 817.251 / 816.878 - within 0.07 m of the converged
levels and with **opposite signs**, so mid-patch, where the pool is, the two agree to
~2 mm (114 m2 / 10.0 m3 against 118 / 10.4). `percolation.water_table_levels` pins
them to the measured values for this case.

Reporting follows: `wetting_report(supported=...)` puts water-table pools in their
own category, out of *film* and *isolated puddles* - such water is standing and
disconnected by construction, so it would otherwise be reported as a defect when it
is exactly what the model intends.

---

## Revision R4 - 2026-08-07 - the gain-lose machinery becomes core aXqua

Everything above was built as a case-specific `percolation:` block that only worked
if the user drew two internal `int-*` lines. It is now the core `gain_lose:` feature
(see CLAUDE.md), with the physical objection that drove the change: **it is fuzzy
where exactly percolation takes place**, so requiring the exchange faces to be drawn
asks for a number nobody has.

### What changed

* **`faces: water-table` (the new default)** derives both faces from the body's
  phreatic surface - a node **loses** where it is wet and its free surface stands
  above the table, **gains** where the table stands above the bed - so nothing is
  drawn. The classification happens **at run time**, so the faces move with the
  stage; a build-time mask cannot do that.
* **`faces: lines`** is what this case keeps: its exchange faces were surveyed and
  calibrated (T6), and the magnitude comes from the lines' own flow column.
* **kf is the calibration parameter** in polygon mode, exposed to HydroBayesCal as
  `f.HMP_KF`. It had to be emitted as a run-time *assignment* rather than a
  `PARAMETER` declaration: HBC matches the text left of the first `=`/`:`, which in
  `DOUBLE PRECISION, PARAMETER :: HMP_KF = ...` is "DOUBLE PRECISION, PARAMETER".
  Verified by replaying HBC's own matcher - the old form matched **0 lines**, the new
  form matches exactly 1, and the rewritten routine still compiles.
* kf values are not textbook constants (1e-9..1e-2 m/s across riverbeds), so the
  config points at Calver (2001), *Riverbed Permeabilities: Information from Pooled
  Data*, Ground Water 39(4):546-553, doi:10.1111/j.1745-6584.2001.tb02343.x.

### The two routes on this bar, measured

| | from the drawn lines | from the polygon alone |
|---|---|---|
| water-table gradient | 7.65 permille | 5.30 permille |
| wetted inside the patch | 119 m2 / 10.4 m3 | 210 m2 / 20.2 m3 |
| patch left dry | 95 % | 91 % |
| losing / gaining face | (pinned to the lines) | 391 m2 / 208 m2 |
| implied exchange at kf=3e-4 | (prescribed 0.065) | **0.213 m3/s** |

The polygon route is self-determining, so at kf=3e-4 it delivers ~3x the calibrated
exchange - which is not a failure of the formulation but the reason kf is exposed for
calibration. **This case therefore stays on `faces: lines` + the lines' flow column**,
so its calibrated behaviour is untouched.

### Migration verified against R3

The config was rewritten in the new spelling and re-run. The build is equivalent -
`steady2d.cas` byte-identical, `user_rain.f` differing only in the two lines that
make kf calibratable - and the run reproduces R3:

| quantity | R3 | migrated | tolerance |
|---|---|---|---|
| flux imbalance | 1.59e-04 | 2.27e-04 | converged < 1e-3, hotstart written |
| **active area** | 7 116 m2 | **7 111 m2** | within 1 % (-0.07 %) |
| stagnant film | 2 508 m2 | 2 517 m2 | within 5 % |
| water-table pool | 99 m2 / 8.2 m3 | 101 m2 / 8.2 m3 | within 5 % |
| baffles (righ US / left US / downstream) | 0.8032 / 1.6003 / 2.3959 | 0.8032 / 1.5990 / 2.3991 | within 1 % |

The old `percolation:` spelling still loads (mapped `mode`->`enabled`,
`losing_region`->`faces`) with a deprecation warning, and `cfg.percolation` remains
an alias - `case-config-freeoutflow.yml` is left on it deliberately, as a live check
that the alias works.

## Open questions / ideas not yet pursued

* **The `porous depth (m)` = 0.5 attribute is currently documentation only.** The
  exchange discharge comes from the `Target flow` column of the `int-*` lines. A
  physically-based alternative would size the exchange from Darcy flow through the
  patch (hydraulic conductivity x gradient x 0.5 m x width) instead of prescribing
  0.065 m3/s. That needs a conductivity estimate for the gravel.
* **Transient storage is not modelled.** The water withdrawn reappears at the
  gaining line in the *same* time step; there is no travel-time lag through the
  patch. For a steady run this is irrelevant; for the unsteady/hydrograph case it
  would matter and would need a small storage reservoir in `USER_RAIN`.
* **The 27.2 % area-jump warning** in the mesh (`growth_ratio` 1.2) is unrelated to
  this problem but worth revisiting - smoother size transitions would help the
  conditioning generally.
* **Disk:** `axqua-case/simulation/` currently holds ~50 GB, most of it the
  failed 2026-07-27 run (a 977 MB `.sortie` plus 15 partition logs of 0.7-1.8 GB
  and several 2.1 GB result parts). It is kept for now as the evidence behind
  attempt 2; delete it once this log is considered sufficient.
