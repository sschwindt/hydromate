"""Example preprocessing for the Inn case.

Clips the DEM(s) declared in a hydromate case configuration to the
region-of-interest boundary, writing the cropped rasters next to their sources.
This is the first step of the workflow; it is followed by ``run2postprocessing.py``.

Run it from anywhere (the paths are resolved from the config file):

    mamba run -n hydromate-env python example-Inn/preprocessing.py
"""

from __future__ import annotations

from pathlib import Path

import hydromate
from hydromate.config import load_config
from hydromate.dem import clip_to_roi

# --------------------------------------------------------------------------- #
# 1. Load the case configuration  (config/<case>.yml)
# --------------------------------------------------------------------------- #
REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "inn.yml"          # <-- your config/<case>.yml

cfg = load_config(CONFIG)


# --------------------------------------------------------------------------- #
# 2. Clip the DEM(s) to the ROI, using the data directories from the YAML
# --------------------------------------------------------------------------- #
def main() -> None:
    print(f"hydromate {hydromate.__version__} — case '{cfg.name}' (EPSG {cfg.crs_epsg})")
    print(f"ROI boundary: {cfg.inputs.boundary}")

    # the DEMs declared in inputs: the initial (required) and the optional target
    dems = [d for d in (cfg.inputs.dem_initial, cfg.inputs.dem_target) if d is not None]
    if not dems:
        raise SystemExit("no DEM declared in inputs.dem_initial / inputs.dem_target")

    for dem in dems:
        out = dem.with_name(f"{dem.stem}-roi-clip.tif")   # written next to the source
        clip_to_roi(dem, cfg.inputs.boundary, out, target_epsg=cfg.crs_epsg)
        print(f"  clipped {dem.name}  ->  {out.name}")

    print("preprocessing done.")


if __name__ == "__main__":
    main()
