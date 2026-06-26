"""Surrogate-assisted Bayesian calibration of the Inn case with HydroBayesCal
(workflow step 3).

Run AFTER ``preprocessing.py`` (builds the case in ``tm-simulation/simulation/``)
and ``initial_run.py`` (confirms it runs). This script calibrates the bed
roughness of the built TELEMAC-2D case against the **FlowTracker velocity ground
truth** and hands the case to HydroBayesCal, writing all calibration artifacts
into the configured ``calibration_dir`` (``tm-simulation/calibration-validation/``).

Calibration target - measured velocity at 0.6*h
-----------------------------------------------
The two SonTek FlowTracker2 survey days
(``user-sources/ground-truth/hydraulics/FlowTracker2-KB15-day{1,2}.xlsx``) hold a
single velocity vertical per point, **measured at 0.6 of the water depth h below
the surface**. By the standard 0.6-depth method that single reading approximates
the *depth-averaged* velocity - exactly what a 2D depth-averaged model resolves -
so it is compared directly to TELEMAC-2D ``SCALAR VELOCITY`` = sqrt(U^2 + V^2).
We therefore form the measured target from the **horizontal** FlowTracker
components only (VelX, VelY; the vertical VelZ is dropped). The measurement
positions come from the DGPS point layers in
``user-sources/geodata/flowtracker2/dgps-flowtracker-day{1,2}_utm33.shp`` (EPSG
25833), reprojected to the project CRS (25832) and joined to the values by ``ID``
- both handled by ``hydromate.ground_truth`` from the ``inputs.ground_truth``
sources already declared in the config.

What it does
------------
1. compiles the FlowTracker ground truth into the tidy hydraulics table;
2. writes the calibration-points CSV (scalar velocity + water depth, with
   per-point error) in the HydroBayesCal schema into ``calibration_dir``;
3. makes sure the built ``.cas`` outputs SCALAR VELOCITY (the ``M`` graphic
   printout), patching it in place if an older build omitted it;
4. emits the HydroBayesCal ``config_Telemac.py`` pointing at the built case;
5. launches HydroBayesCal's ``bal_telemac.py`` on it.

HydroBayesCal lives in its own checkout/environment (it needs bayesvalidrox,
gpytorch, ...), so step 5 is run in a subshell that first sources TELEMAC and
then uses that environment. Point it at your setup with the env vars
``HYDROBAYESCAL_DIR`` (the repo holding ``bal_telemac.py``) and
``HYDROBAYESCAL_ENV`` (the conda env), or the ``--hbc-dir`` / ``--hbc-env`` flags.
Use ``--prepare-only`` to write the CSV + config without launching.

Run: mamba run -n hydromate-env python cases/example-Inn/run_Bayes_cal.py
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from hydromate import compile_ground_truth, read_tidy
from hydromate.calibration import emit_hbc_config
from hydromate.config import load_config

CONFIG = Path(__file__).resolve().parent / "case-config.yml"
cfg = load_config(CONFIG)

# calibration target: the FlowTracker velocity at 0.6*h ~ depth-averaged velocity,
# compared to TELEMAC-2D SCALAR VELOCITY. Water depth is extracted too (for the
# calibration assessment); switch the calibration target by editing this list.
CALIBRATION_QUANTITIES = ["SCALAR VELOCITY"]
EXTRACTION_QUANTITIES = ["WATER DEPTH", "SCALAR VELOCITY"]

# per-point site-specific (instrument) error written to the <QTY>_ERROR columns,
# in physical units. HydroBayesCal adds this in quadrature to its built-in 10%
# measurement + 10% surrogate error, so a small floor avoids a degenerate
# (zero-variance) likelihood at low-velocity / shallow points.
VELOCITY_ERROR_FLOOR = 0.01   # m/s
DEPTH_ERROR_FLOOR = 0.02      # m (DGPS/staff depth precision)

# where HydroBayesCal is installed (overridable via env var or --hbc-dir/--hbc-env)
HBC_DIR = Path(os.environ.get("HYDROBAYESCAL_DIR", "/home/schwindt/github/hydrobayescal"))
HBC_ENV = os.environ.get("HYDROBAYESCAL_ENV", "wrr-proj")


def build_velocity_calibration_csv(cfg) -> tuple[Path, pd.DataFrame]:
    """Compile the FlowTracker ground truth and write the calibration-points CSV.

    Columns follow the HydroBayesCal schema, which reads the first four columns by
    position (id, x, y, z) and the quantity columns by name
    (``<QTY>_DATA`` / ``<QTY>_ERROR``).
    """
    compile_ground_truth(cfg)                       # tidy table from inputs.ground_truth
    tables = read_tidy(cfg.ground_truth_path)
    if "hydraulics" not in tables:
        raise SystemExit(
            f"no 'hydraulics' tab in {cfg.ground_truth_path}. Check the FlowTracker "
            "sources under inputs.ground_truth in case-config.yml."
        )
    df = tables["hydraulics"].reset_index(drop=True)
    for col in ("x", "y", "u", "v", "h"):
        if col not in df.columns:
            raise SystemExit(f"hydraulics ground truth is missing the '{col}' column "
                             f"(has {list(df.columns)})")

    u, v, h = df["u"].astype(float), df["v"].astype(float), df["h"].astype(float)
    # depth-averaged proxy from the horizontal components (drop vertical VelZ/w)
    velocity = np.hypot(u, v)
    # propagate the FlowTracker per-component errors: sigma_V = sqrt((u*su)^2+(v*sv)^2)/V
    su = df["u_err"].astype(float) if "u_err" in df.columns else pd.Series(0.0, index=df.index)
    sv = df["v_err"].astype(float) if "v_err" in df.columns else pd.Series(0.0, index=df.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        v_err = np.sqrt((u * su) ** 2 + (v * sv) ** 2) / velocity.replace(0.0, np.nan)
    v_err = v_err.fillna(0.0).clip(lower=VELOCITY_ERROR_FLOOR)

    out = pd.DataFrame({
        "id": np.arange(1, len(df) + 1),
        "x": df["x"].astype(float),
        "y": df["y"].astype(float),
        "z": df["z"].astype(float) if "z" in df.columns else 0.0,
        "SCALAR VELOCITY_DATA": velocity.round(6),
        "SCALAR VELOCITY_ERROR": v_err.round(6),
        "WATER DEPTH_DATA": h.round(6),
        "WATER DEPTH_ERROR": np.full(len(df), DEPTH_ERROR_FLOOR),
    })
    path = cfg.calibration_path(cfg.calibration_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return path, out


def ensure_scalar_velocity_output(cas_path: Path) -> bool:
    """Make sure the built ``.cas`` writes SCALAR VELOCITY (graphic printout ``M``).

    HydroBayesCal reads SCALAR VELOCITY straight from the results SELAFIN, so it
    must be in VARIABLES FOR GRAPHIC PRINTOUTS. Fresh builds already include it;
    this patches an older case in place. Returns True if the file was changed.
    """
    if not cas_path.exists():
        raise SystemExit(
            f"no built case at {cas_path}. Run preprocessing.py (and initial_run.py) first."
        )
    text = cas_path.read_text()
    m = re.search(r"(VARIABLES FOR GRAPHIC PRINTOUTS\s*:\s*')([^']*)(')", text)
    if not m:
        print(f"  warning: no VARIABLES FOR GRAPHIC PRINTOUTS line in {cas_path.name}; "
              "cannot verify SCALAR VELOCITY output")
        return False
    codes = [c.strip() for c in m.group(2).split(",") if c.strip()]
    if "M" in codes:
        return False
    codes.append("M")
    patched = text[:m.start()] + m.group(1) + ",".join(codes) + m.group(3) + text[m.end():]
    cas_path.write_text(patched)
    print(f"  added scalar velocity ('M') to {cas_path.name} graphic printouts")
    return True


def run_hydrobayescal(config_path: Path, hbc_dir: Path, hbc_env: str) -> int:
    """Launch HydroBayesCal's bal_telemac.py on the emitted config.

    Runs in a login subshell that sources TELEMAC (so the solver is reachable)
    and then uses the HydroBayesCal conda environment.
    """
    driver = hbc_dir / "bal_telemac.py"
    if not driver.is_file():
        raise SystemExit(
            f"HydroBayesCal driver not found: {driver}\n"
            "Set HYDROBAYESCAL_DIR (or pass --hbc-dir) to the hydrobayescal checkout "
            "that contains bal_telemac.py."
        )
    pysource = cfg.telemac.pysource
    inner = (f"source {shlex.quote(str(pysource))}; "
             f"cd {shlex.quote(str(hbc_dir))}; "
             f"mamba run -n {shlex.quote(hbc_env)} python bal_telemac.py "
             f"--config {shlex.quote(str(config_path))}")
    print("\nlaunching HydroBayesCal:\n  " + inner + "\n")
    return subprocess.run(["bash", "-lc", "set -e; " + inner]).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prepare-only", action="store_true",
                        help="write the calibration CSV + config_Telemac.py but do "
                             "not launch HydroBayesCal")
    parser.add_argument("--hbc-dir", type=Path, default=HBC_DIR,
                        help=f"HydroBayesCal checkout (default: {HBC_DIR})")
    parser.add_argument("--hbc-env", default=HBC_ENV,
                        help=f"conda env HydroBayesCal runs in (default: {HBC_ENV})")
    args = parser.parse_args()

    cfg.ensure_dirs()
    # use the velocity ground truth for this calibration
    cfg.calibration.calibration_quantities = CALIBRATION_QUANTITIES
    cfg.calibration.extraction_quantities = EXTRACTION_QUANTITIES

    print(f"calibration target(s): {CALIBRATION_QUANTITIES}  "
          f"parameters: {[p.name for p in cfg.calibration.parameters]}")

    csv, table = build_velocity_calibration_csv(cfg)
    print(f"calibration points ({len(table)}) -> {csv}")
    print(f"  scalar velocity [m/s]: {table['SCALAR VELOCITY_DATA'].min():.3f} .. "
          f"{table['SCALAR VELOCITY_DATA'].max():.3f}; "
          f"water depth [m]: {table['WATER DEPTH_DATA'].min():.3f} .. "
          f"{table['WATER DEPTH_DATA'].max():.3f}")

    ensure_scalar_velocity_output(cfg.model_path(cfg.cas_file))

    config_path = emit_hbc_config(cfg, csv)
    print(f"HydroBayesCal config -> {config_path}")

    if args.prepare_only:
        print("\n--prepare-only: skipping the HydroBayesCal launch. Run it with:")
        print(f"  cd {args.hbc_dir} && python bal_telemac.py --config {config_path}")
        return

    rc = run_hydrobayescal(config_path, args.hbc_dir, args.hbc_env)
    if rc == 0:
        print(f"\nHydroBayesCal finished. Results in {cfg.calibration_dir}")
    else:
        print(f"\nHydroBayesCal exited with code {rc}. See the output above and "
              f"{cfg.calibration_dir}.")


if __name__ == "__main__":
    main()
