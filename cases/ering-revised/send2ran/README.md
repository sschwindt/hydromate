## Requirements

The steering files are designed to work with Telemac v9.0 or newer, and they will crash with older versions (i.e., <=v8p5). I recommend you use the latest Telemac version (v9.1.1), which you can easily install on Linux-based computers with our auto-installer scripts, see:
https://hydro-informatics.com/install-telemac-autoinstaller/

## Files

**IMPORTANT:** All files must be stored in a system path that does **not contain any spaces**!

* steady2d.cas represents the initial simulation that is needed to generate r2d.slf; it is here FYI but you should not need to run it because r2d.slf already exists.

* the files that you need for the 2d-3d comparison are:

  * r2d.slf (the hotstart conditions)
  * geometry.slf (the computational mesh)
  * boundaries.cli (geometric boundary definition file)
  * steady2d.cas (not so important: this is the spin-up simulation for 2d-hotstarting)
  * hotstart2d.cas (very important: this is the file you use to hotstart 2d simulations)
  * spinup3d_stage1.cas (not so important: this is a link file to speed up 3d simulations)
  * hotstart3d_stage2.cas (very important: this is the file you use to hotstart 3d simulations)

* other files in this folder:
  * `convergence/` contains files for flow convergence check-ups to answer the question whether your simulation ran long enough so that inflow and outflow converged (to the right value)
  * `r*.slf` are results files that result from spin-up simulation (required for hotstarts)

> Note that I only set one global ks value (0.07) for bottom roughness (friction) because roughness zones are only applicable in 2d, so the 3d simulation would be different from the 2d simulation just because of roughness zones, which we do not want (we said to focus on the difference between 2d/3d non-hydrostatic choice).

## Usage

The simulation achieves quite short runtimes through double spin-up simulations, one for 2d hotstarts, one for 3d hotstarts. Details are explained in this section. If you encounter long runtimes, I recommend you look into the `convergence/` directory. Note that I put the `-s` flag in all simulations to write the `.sortie` file out for flux convergence recording. You can use `convergence/convergence_plotter_relative.py` to check into the convergence, for instance, with:

```bash
python3 convergence_plotter_relative.py hotstart2d.cas_2026-07-01-15h19min17s.sortie --csv hotstart2d-dQ.csv
```

The resulting plots are a little buggy, so I recommend you look into the `.csv` files.

### Launch Telemac config

Depending on the local installation script, activate your Telemac environment with, for example:

```bash
source ~/opt/tm-v911/configs/pysource.debian12.sh
```

### Run a 2d simulation (hotstart)


```bash
telemac2d.py hotstart2d.cas --ncsize=16 -s --nozip
```

> Note (1): I tested this with 16 cores, which takes ~9 seconds; you may want to increase that number (not sure up to which point that is stable).
> Note (2): before running hotstart2d.cas, I ran `telemac2d.py steady2d.cas --ncsize=16`, which took approx. 22 minutes.

### Run a 3d simulation (additional spin-up + hotstart)

The 3d setup uses a two-stage spin-up workflow:

1. **Stage 1** performs a cheaper hydrostatic 3d spin-up from the converged 2d result.
2. **Stage 2** continues from the stage 1 3d result and performs the actual non-hydrostatic 3d run.

I already ran stage 1 with, and you won't need to do that unless you modify something groundbreaking in the 2d files; either way, you can re-run the stage 1 with the following command though **stage 1 is not intended for use in Bayesian loops**:

```bash
telemac3d.py spinup3d_stage1.cas --ncsize=16 -s --nozip
```

> Note that I largely exaggerated with the 3d spinup in the preparation phase and let it run over night for 4H16MIN19SEC, so the really important non-hydrostatic 3d simulations converge faster.

The result of this is `r3d-stage1.slf` for use in **stage 2**, which **is the actual simulation steering file that you want to use in the Bayesian optimization context**. To run run stage 2 tap:

```bash
telemac3d.py hotstart3d_stage2.cas --ncsize=16 -s --nozip
```

The result of this is `r3d-dyn.slf`, and it took ~17 minutes with 16 cores; you may want to increase the number of cores though I am not sure up to which point that is stable.

> Note that the convergence analysis shows that it might be sufficient to use `NUMBER OF TIME STEPS : 4000` but I strongly recommend to keep `NUMBER OF TIME STEPS : 5000`, which adds way more certainty about the simulation.

For your purpose, if you want to modify a numerical model parameter for the actual 3d simulation, change it in the **stage 2** steering file. Keep the stage 1 output/continuation lines unchanged so stage 2 continues from the hydrostatic spin-up state.


### Output

The output parameters include mean velocity components, Froude number (F), **TKE (K)**, and its dissispation rate (2d=E; 3d=EPS). For 3d, I also added the turbulent eddy viscosity (X and Z dirs.) with `NUX,NUZ`; those parameters might be interesting with regard to investigating how strongly the turbulence model steps into the simulation. That is, the higher `NUX,NUZ`, the less turbulence is resolved, so if you enabled the `DYNAMIC PRESSURE IN WAVE EQUATION` keyword in `hotstart3d_stage2.cas`, you should yield lower `NUX,NUZ`. This is, in essence, one of the key observations in the [paper with Sergio](https://onlinelibrary.wiley.com/doi/abs/10.1029/2022WR033660). Still, for your purposes, the most important outputs are the **mean velocity components (U+V in 2d, U+V+W in 3d) and TKE (K)**.


## More options

### Simulation cutoff

To reduce computing time, you may want to cut off simulations by modifying the second line in the following code block (i.e., raise the stop criteria). I set it to 3.E-4;3.E-4;1.E-3 (default: 1.E-4) because that is sufficient. The criteria refer to velocity, water depth, and tracer (not applicable), respectively; so depth is the most critical thing here. To speed up the simulations you may there want to set the first and particularly second entries to at max. 1.E-3.

```hotstart2d.cas
STOP IF A STEADY STATE IS REACHED : YES
STOP CRITERIA : 3.E-4;3.E-4;1.E-3 / velocity; water depth; tracer
```

> Note that these settings are **very sensitive** and changing them can greatly affect **runtime and output accuracy**; this is also coupled with the keyword `DESIRED COURANT NUMBER`. If you experience too long runtime for this hotstart2d simulation, recommend you deactivate the aboce block in the `hotstart2d.cas`.

Telemac3d does not have these keywords ready, so I ran the hotstart file with the keyword `INFORMATION ABOUT MASS-BALANCE FOR EACH LISTING PRINTOUT: YES`, which makes Telemac write a `.sortie` file. That file can then be used with `convergence/convergence_plotter_relative.py` (pre-set for this case) to check when inflow and outflow converge; note that dQ(in-out) should be less than 1.E-3 for convergence. Based on that plot, I set the simulation duration in `hotstart3d_stage2.cas` to:

```hotstart3d_stage2.cas
TIME STEP : 0.5
NUMBER OF TIME STEPS : 5000
GRAPHIC PRINTOUT PERIOD : 1000
LISTING PRINTOUT PERIOD : 500
```

You may also just want to increase the printout periods (both graphic and listing) because writing the output takes time.

### Extended 3d options

If you want to set a rather dull option (i.e., a benchmark for which the calibration **MUST NOT perform better than for the 2d** case), force the 3d solver into shallow-water equations by directly using `spinup3d_stage1.cas`, which contains:

```spinup3d_stage1.cas
NON-HYDROSTATIC VERSION : NO
```

In turn, you can also try to **improve 3d performance** with the following option (I did not test the runtime though):

```hotstart3d_stage2.cas
DYNAMIC PRESSURE IN WAVE EQUATION : YES
```

Note that we are already using a pressure Poisson equation solver (PPE=7), which you can modify, too, just for fun.

### Merge a crashed simulation

To merge a crashed simulation, use GRETEL with the following script (replace the timestamp <MM-DD-17h28min17s>):

```bash
runcode.py --merge -w steady2d.cas_2026-MM-DD-17h28min17s --ncsize 8 telemac2d hotstart2d.cas
```





