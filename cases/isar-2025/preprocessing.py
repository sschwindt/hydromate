"""Preprocessing + case build (TEMPLATE, workflow step 1).

Assembles a complete, ready-to-run TELEMAC-2D case at the steady discharge set in
``case-config.yml`` (``boundaries.prescribed_flowrate``): clips the DEM(s), builds
the mesh (anisotropic + roughness), classifies the liquid boundaries and writes the
case into ``hydromate-case/simulation/`` -- the final mesh ``geometry.slf``, the
boundary-conditions ``boundaries.cli``, the friction ``friction.tbl`` and the steering
``steady2d.cas`` -- plus the HydroBayesCal artifacts in ``calibration-validation/`` and
the ground-truth / DEM-clip products in ``preprocessing/``. A constant inflow series and
the outflow stage-discharge rating curve are synthesised if missing.

The inflow Q (m3/s) and the outflow stage (H) prescription come from THIS case's
config, never from a value hard-coded here (see ``hydromate.prepare_steady_inputs``).

It does NOT launch the solver: run ``initial_run.py`` next to test-run the built
case (that confirms it does not crash, ending the preprocessing step). Then run
``mesh_convergence_study.py``, and finally HydroBayesCal.

Run: mamba run -n hydromate-env python cases/<your-case>/preprocessing.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from hydromate import pipeline, prepare_steady_inputs, setup_logging
from hydromate.config import load_config

# optional CLI arg selects the scenario config, e.g.
#   python preprocessing.py case-config-greenampt.yml
CONFIG = Path(__file__).resolve().parent / (
    sys.argv[1] if len(sys.argv) > 1 else "case-config.yml")
cfg = load_config(CONFIG)


def main() -> None:
    cfg.ensure_dirs()
    # the mesh-convergence study (step 2) writes into this folder; create it now
    # so it already exists in the produced hydromate-case/ tree after preprocessing
    cfg.postprocessing_path("mesh-convergence").mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.model_path(cfg.log_file))   # build log in simulation/
    print(f"case '{cfg.name}' -> building into {cfg.model_dir}")

    # steady discharge + outflow rating from THIS case's config (inflow/rating
    # synthesised only if missing); dry start left untouched (production initial run)
    q = prepare_steady_inputs(cfg)
    print(f"building at the configured steady discharge Q={q:g} m3/s")

    try:
        art = pipeline.run(cfg, validate_env=False, dry_run=False)
    except Exception as exc:  # noqa: BLE001 - report what is still missing
        print(f"build not ready: {type(exc).__name__}: {exc}")
        print("complete the inputs and telemac.pysource in case-config.yml, then re-run.")
        return

    # keep a copy of the rating curve next to the case for traceability
    if cfg.boundaries.stage_discharge and Path(cfg.boundaries.stage_discharge).exists():
        shutil.copy(cfg.boundaries.stage_discharge, cfg.model_path("rating-curve.csv"))

    print(f"\nbuilt the TELEMAC case in {cfg.model_dir}:")
    print(f"  mesh      : {art.geometry_slf.name}")
    print(f"  boundary  : {art.boundary_cli.name}")
    print(f"  friction  : {art.friction_tbl.name}")
    print(f"  steering  : {art.cas_file.name}")
    if art.hbc_config:
        print(f"  HBC config: {art.hbc_config}")
    else:
        print("  HBC config: skipped (ground-truth data do not match; see hydromate.log)")
    print(f"  convergence dir ready: {cfg.postprocessing_path('mesh-convergence')}")
    print("next: test-run it with  python cases/<your-case>/initial_run.py")


if __name__ == "__main__":
    main()
