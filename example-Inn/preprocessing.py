"""Example preprocessing for the Inn case: clip the DEM(s) to the ROI.

First step of the workflow (followed by ``run2postprocessing.py``). The script
reads a hydromate case configuration and clips the DEM(s) it declares to the
region-of-interest boundary, writing each cropped raster next to its source as
``<name>-roi-clip.tif``.

Run it with:

    mamba run -n hydromate-env python example-Inn/preprocessing.py
"""

from pathlib import Path

import hydromate
from hydromate.config import load_config

# ---------------------------------------------------------------------------
# Load the case configuration  (config/<case>.yml)
# ---------------------------------------------------------------------------
config_file = Path(__file__).resolve().parents[1] / "config" / "inn.yml"
cfg = load_config(config_file)

print(f"Case '{cfg.name}'  |  CRS EPSG:{cfg.crs_epsg}")
print(f"ROI boundary: {cfg.inputs.boundary.name}")

# ---------------------------------------------------------------------------
# Clip the DEM(s) to the region of interest
# (uses the boundary + CRS from the config; writes <name>-roi-clip.tif next
#  to each source DEM)
# ---------------------------------------------------------------------------
initial_clip = hydromate.clip_dem_to_roi(cfg, cfg.inputs.dem_initial)
print(f"initial DEM clipped -> {initial_clip}")

if cfg.inputs.dem_target is not None:
    target_clip = hydromate.clip_dem_to_roi(cfg, cfg.inputs.dem_target)
    print(f"target  DEM clipped -> {target_clip}")

print("preprocessing done.")
