"""Example: build the TELEMAC case and run it through to postprocessing.

Second step of the workflow, after ``preprocessing.py``. From the same case
configuration this:

1. builds a calibration-ready TELEMAC-2D case with hydromate (mesh + geometry
   ``.slf``, boundary ``.cli``, steering ``.cas``, friction ``.tbl``, the
   calibration-points CSV and a HydroBayesCal ``config_Telemac.py``);
2. optionally runs the solver once as a sanity check (``dry_run=True``);
3. hands off to HydroBayesCal for the surrogate-assisted Bayesian calibration
   and postprocessing.

A full build needs every input in the config to exist (liquid boundaries,
inflow, ...) and ``telemac.pysource`` to point at a real TELEMAC environment, so
this script validates first and explains what is still missing if it is not
ready.

Run it from anywhere:

    mamba run -n hydromate-env python example-Inn/run2postprocessing.py
"""

from __future__ import annotations

from pathlib import Path

from hydromate import pipeline
from hydromate.config import load_config

# --------------------------------------------------------------------------- #
# 1. Load the case configuration  (config/<case>.yml)
# --------------------------------------------------------------------------- #
REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "inn.yml"          # <-- your config/<case>.yml

cfg = load_config(CONFIG)


def main() -> None:
    print(f"case '{cfg.name}' -> model dir {cfg.model_dir}")

    # ---- check the config is complete before doing any heavy work ----------
    try:
        cfg.validate()
    except Exception as exc:  # noqa: BLE001 - report and stop cleanly
        print(f"config not ready for a full build: {type(exc).__name__}: {exc}")
        print("complete the inputs and telemac.pysource in the config, then re-run.")
        return

    # ---- 2. build the TELEMAC case -----------------------------------------
    # set dry_run=True to also launch the solver once and confirm the case runs
    artifacts = pipeline.run(cfg, validate_env=True, dry_run=False)
    print(f"built case in {cfg.model_dir}")
    print(f"  geometry : {artifacts.geometry_slf}")
    print(f"  steering : {artifacts.cas_file}")
    print(f"  HBC cfg  : {artifacts.hbc_config}")

    # ---- 3. calibrate / postprocess with HydroBayesCal ---------------------
    # The build emits a config_Telemac.py; run the calibration with it:
    #
    #   cd <model_dir>
    #   python /path/to/hydrobayescal/bal_telemac.py --config config_Telemac.py
    #
    print("next: calibrate with HydroBayesCal using", artifacts.hbc_config)


if __name__ == "__main__":
    main()
