"""Surrogate-assisted Bayesian calibration of the built case (workflow step 3).

Thin wrapper around :func:`hydromate.run_single_flow_calibration` - all the
logic lives in ``hydromate.bayescal``. Run AFTER ``preprocessing.py`` (builds the
case) and ``initial_run.py`` (confirms it runs). It compiles the FlowTracker
velocity ground truth (from ``ground_truth.sources`` in case-config.yml) into the
HydroBayesCal calibration-points CSV, makes sure the ``.cas`` prints SCALAR
VELOCITY, emits ``config_Telemac.py`` and launches HydroBayesCal.

The calibration target is the FlowTracker velocity at 0.6*h (~ the depth-averaged
velocity a 2D model resolves), compared to TELEMAC-2D ``SCALAR VELOCITY``. Point
at your HydroBayesCal install with HYDROBAYESCAL_DIR / HYDROBAYESCAL_ENV (or the
--hbc-dir / --hbc-env flags).

Run: mamba run -n hydromate-env python <case>/run_Bayes_cal.py [--prepare-only]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hydromate import hbc_dir, hbc_env, run_single_flow_calibration
from hydromate.config import load_config

CONFIG = Path(__file__).resolve().parent / "case-config.yml"

# calibration target (edit to switch quantity); water depth is extracted alongside
CALIBRATION_QUANTITIES = ["SCALAR VELOCITY"]
EXTRACTION_QUANTITIES = ["WATER DEPTH", "SCALAR VELOCITY"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prepare-only", action="store_true",
                        help="write the CSV + config but do not launch HydroBayesCal")
    parser.add_argument("--hbc-dir", type=Path, default=hbc_dir(),
                        help="HydroBayesCal checkout (default: $HYDROBAYESCAL_DIR)")
    parser.add_argument("--hbc-env", default=hbc_env(),
                        help="conda env HydroBayesCal runs in (default: $HYDROBAYESCAL_ENV)")
    args = parser.parse_args()

    cfg = load_config(CONFIG)
    return run_single_flow_calibration(
        cfg, calibration_quantities=CALIBRATION_QUANTITIES,
        extraction_quantities=EXTRACTION_QUANTITIES,
        prepare_only=args.prepare_only, checkout=args.hbc_dir, env=args.hbc_env)


if __name__ == "__main__":
    raise SystemExit(main())
