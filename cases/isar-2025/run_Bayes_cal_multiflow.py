"""Multi-discharge Bayesian calibration (workflow step 3, multi-flow) - TEMPLATE.

Thin wrapper around :func:`axqua.run_multiflow_calibration`. Calibrates one
shared roughness against velocity ground truth from SEVERAL steady-discharge
field campaigns jointly (each campaign = one TELEMAC run per collocation point).
All logic lives in ``axqua.bayescal`` / ``axqua.campaigns``; this script
only declares the case's list of :class:`~axqua.FlowSpec`.

Delete this file if the case has a single discharge (use run_Bayes_cal.py). Fill
FLOWS with one entry per campaign; each ``kind`` picks the FlowTracker layout:

* ``adapter``  - a summary workbook (one row per vertical) + a DGPS point layer
  joined by ID (``positions=``); the standard path.
* ``transect`` - a taped cross-section (Station ``<vertical>-<sub>``) + a point
  GeoPackage keyed by vertical number (``positions=``).
* ``inline``   - verticals with EPSG coordinates repeated inline on each row.

Raise ``vel_err_floor`` for a campaign that is not an independent representative
sample of the 2D flow (e.g. one dense cross-section) so it does not dominate the
joint likelihood. Prefer a WATER DEPTH / flood-extent target over wadeable
velocities for a high flow whose points sit in shallow margins.

Modes: --smoke | --run | --resume (default: prepare without launching).
Requires the built case (preprocessing.py) and the hydrobayescal checkout
(installed package; --hbc-checkout overrides with a source tree).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from axqua import FlowSpec, run_multiflow_calibration
from axqua.config import load_config

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "case-config.yml"
GT = HERE / "user-sources/ground-truth/hydraulics"
GEO = HERE / "user-sources/geodata"

# One FlowSpec per steady-discharge campaign (edit for this case):
FLOWS = [
    # FlowSpec(name="qLOW", discharge=47.3, kind="adapter", duration=1500.0,
    #          values=GT / "FT_summary.xlsx",
    #          positions=GEO / "flowtracker/dgps-low.gpkg"),
    # FlowSpec(name="qMID", discharge=48.45, kind="transect", duration=1500.0,
    #          values=GT / "FT_summary_mid.xlsx",
    #          positions=GEO / "TKE_mid.gpkg", vel_err_floor=0.05),
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
    parser.add_argument("--force", action="store_true")
    # HydroBayesCal is a pip dependency and runs in this interpreter; a checkout is
    # only needed to develop against an unreleased driver.
    parser.add_argument("--hbc-checkout", type=Path, default=None,
                        help="use drivers from a HydroBayesCal source checkout "
                             "instead of the installed package")
    args = parser.parse_args()

    if not FLOWS:
        raise SystemExit("edit FLOWS in this script: add one FlowSpec per campaign.")
    launch_mode = ("smoke" if args.smoke else "run" if args.run
                   else "resume" if args.resume else "prepare")
    cfg = load_config(CONFIG)
    return run_multiflow_calibration(cfg, FLOWS, launch_mode=launch_mode,
                                     checkout=args.hbc_checkout,
                                     force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
