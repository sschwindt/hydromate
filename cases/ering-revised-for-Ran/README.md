## Requirements

The steering files are designed to work with Telemac v9.0 or newer, and they will crash with older versions (i.e., <=v8p5). I recommend you use the latest Telemac version (v9.1.1), which you can easily install on Linux-based computers with our auto-installer scripts, see:
https://hydro-informatics.com/install-telemac-autoinstaller/

## Files

**IMPORTANT:** All files must be stored in a system path that does not contain any spaces!

* steady2d.cas represents the initial simulation that is needed to generate r2d.slf; it is here FYI but you should not need to run it because r2d.slf already exists.

* the files that you need for the 2d-3d comparison are:

  * r2d.slf (the hotstart conditions)
  * geometry.slf (the computational mesh)
  * boundaries.cli (geometric boundary definition file)
  * hotstart2d.cas (very important: this is the file you use to hotstart 2d simulations)
  * hotstart3d.cas (very important: this is the file you use to hotstart 3d simulations)

## Usage

### Launch Telemac config

Depending on the local installation script, activate your Telemac environment with, for example:

```bash
source ~/opt/tm-v911/configs/pysource.debian12.sh
```

### Run a 2d simulation (hotstart)

```bash
telemac2d.py hotstart2d.cas --ncsize=8
```

> Note that I tested this with 8 cores only, which takes ~22 minutes; you may want to increase that number (not sure up to which point that is stable).

### Run a 3d simulation (hotstart)

```bash
telemac3d.py hotstart3d.cas --ncsize=8
```

> Note that I tested this with 8 cores only, which takes ~XX minutes; you may want to increase that number (not sure up to which point that is stable).

## More options

### Simulation cutoff

To reduce computing time, you may want to cut off simulations by modifying the second line in the following code block (i.e., raise the stop criteria). I set it initially to 1.E-3 (default: 1.E-4) because I think that is totally sufficient.

```hotstart2/3d.cas
STOP IF A STEADY STATE IS REACHED : YES
STOP CRITERIA : 1.E-3;1.E-3;1.E-3
```

### Extended 3d options

If you want to set a rather dull option (i.e., a benchmark for which the calibration **MUST NOT perform better than for the 2d** case), force the 3d solver into shallow-water equations by setting:

```hotstart3d.cas
NON-HYDROSTATIC VERSION : NO
```

In turn, you can also try to **improve 3d performance** with the following option (I did not test the runtime though):

```hotstart3d.cas
DYNAMIC PRESSURE IN WAVE EQUATION : YES
```

Note that we are already using a pressure Poisson equation solver (PPE=7), which you can modify, too, just for fun.

### Merge a crashed simulation

To merge a crashed simulation, use GRETEL with the following script (replace the timestamp <MM-DD-17h28min17s>):

```bash
runcode.py --merge -w steady2d.cas_2026-MM-DD-17h28min17s --ncsize 8 telemac2d hotstart2d.cas
```

