"""Multi-discharge Bayesian calibration of the KB15 case (workflow step 3, multi).

Thin wrapper around :func:`hydromate.run_multiflow_calibration` - all the logic
lives in ``hydromate.bayescal`` / ``hydromate.campaigns``. Calibrates one shared
channel roughness against velocity ground truth from several steady-discharge
FlowTracker campaigns at once (each campaign = one TELEMAC run per collocation
point, joined into one Bayesian inference by the additive
``bal_telemac_multiflow.py`` + ``MultiflowTelemacModel`` in the hydrobayescal
checkout).

Campaigns (see user-sources/ground-truth/hydraulics/discharge-info.md):

* **Sept 2025, Q=47.3 m3/s** - revised summary workbook, 30 reach-spanning pts.
* **Nov 2025, Q=48.45 m3/s** - one taped cross-section (22 verticals in ~10 m).
  Its per-point velocity error is floored high (0.05 m/s) so this single, model-
  over-predicted section informs but does not dominate the joint likelihood.
* **March 2026, Q=168 m3/s - EXCLUDED.** FlowTracker is wadeable, so at 168 m3/s
  it only samples shallow slow margins (~0.58 m, 0.21 m/s) that a 2D model routes
  fast (~1.1 m/s) - a ~5x gap roughness cannot close. Left commented below; a
  high flow should enter via WATER DEPTH / flood-extent, not wadeable velocities.

Modes: ``--smoke`` (isolated plumbing test), ``--run`` (full: initial design +
BAL), ``--resume`` (reuse completed per-flow initial designs, only_bal_mode);
default writes everything without launching. HydroBayesCal install via
HYDROBAYESCAL_DIR / HYDROBAYESCAL_ENV.

Run: mamba run -n hydromate-env python cases/inn-KB15-2025-hydro/run_Bayes_cal_multiflow.py [--smoke|--run|--resume]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hydromate import FlowSpec, hbc_dir, hbc_env, run_multiflow_calibration
from hydromate.config import load_config

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "case-config.yml"
GT = HERE / "user-sources/ground-truth/hydraulics"
GEO = HERE / "user-sources/geodata"

# case-specific flow set (the only thing this script declares).
# Since July 2026 the targets come pre-compiled by prepare_corrected_targets.py
# (kind "csv"): pole-corrected DGPS z, bathymetry-corrected water-level depth
# targets, profile-averaged velocities with a structural discrepancy term -
# see README.md "Data particularities". Run that script first after any
# ground-truth change. (The raw adapter/transect specs are kept below for
# reference, commented out.)
PREP = HERE / "hydromate-case/preprocessing"
FLOWS = [
    FlowSpec(name="q47-3", discharge=47.3, kind="csv", duration=1500.0,
             values=PREP / "measurements-corrected-q47-3.csv"),
    FlowSpec(name="q48-45", discharge=48.45, kind="csv", duration=1500.0,
             values=PREP / "measurements-corrected-q48-45.csv"),
    # FlowSpec(name="q47-3", discharge=47.3, kind="adapter", duration=1500.0,
    #          values=GT / "FT_TKE_Summary.xlsx",
    #          positions=GEO / "flowtracker2/dgps-flowtracker-kb15-sept25-zcorrected.gpkg"),
    # FlowSpec(name="q48-45", discharge=48.45, kind="transect", duration=1500.0,
    #          values=GT / "FT_TKE_Summary_Nov25.xlsx",
    #          positions=GEO / "TKE_KB15_Nov25.gpkg", vel_err_floor=0.05),
    # EXCLUDED (wadeable margins not comparable to modelled channel velocity):
    # FlowSpec(name="q168", discharge=168.0, kind="inline", duration=3000.0,
    #          values=GT / "FT_TKE_Summary_March26.xlsx"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true",
                      help="isolated end-to-end plumbing test (tiny runs)")
    mode.add_argument("--run", action="store_true",
                      help="launch the full calibration (initial design + BAL)")
    mode.add_argument("--resume", action="store_true",
                      help="reuse completed per-flow initial designs (only_bal_mode)")
    parser.add_argument("--force", action="store_true",
                        help="launch even if another calibration appears active")
    parser.add_argument("--hbc-dir", type=Path, default=hbc_dir())
    parser.add_argument("--hbc-env", default=hbc_env())
    args = parser.parse_args()

    launch_mode = ("smoke" if args.smoke else "run" if args.run
                   else "resume" if args.resume else "prepare")
    cfg = load_config(CONFIG)
    return run_multiflow_calibration(cfg, FLOWS, launch_mode=launch_mode,
                                     checkout=args.hbc_dir, env=args.hbc_env,
                                     force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
