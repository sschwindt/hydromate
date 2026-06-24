"""Example preprocessing for the Inn case.

First step of the workflow (followed by ``run2postprocessing.py``). From a
hydromate case configuration this script:

1. clips the DEM(s) to the ROI boundary (``<name>-roi-clip.tif`` next to each
   source DEM);
2. generates the computational mesh (anisotropic, flow-aligned in the channel)
   and stores the bare geometry as ``mesh-raw.slf``;
3. interpolates DEM elevations onto the mesh nodes (rounded to 4 decimals) and
   stores the result as ``mesh-elevations.slf``;
4. compiles the field ground truth into the tidy calibration table.

Run it with:

    mamba run -n hydromate-env python example-Inn/preprocessing.py
"""

import logging
from pathlib import Path

import hydromate
from hydromate.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")

# ---------------------------------------------------------------------------
# Load the case configuration  (config/<case>.yml)
# ---------------------------------------------------------------------------
config_file = Path(__file__).resolve().parents[1] / "config" / "inn.yml"
cfg = load_config(config_file)
cfg.ensure_dirs()

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

# ---------------------------------------------------------------------------
# Generate the computational mesh from the boundary + mesh zones + centerline
# (anisotropic, flow-aligned in the channel). Store the bare geometry first.
# ---------------------------------------------------------------------------
mesh = hydromate.build_mesh(cfg)
raw_slf = hydromate.write_mesh(mesh, cfg.work_path("mesh-raw.slf"),
                               title=f"{cfg.name} raw mesh")
print(f"mesh generated -> {raw_slf}  ({mesh.npoin} nodes, {mesh.nelem} elements)")

# ---------------------------------------------------------------------------
# Interpolate DEM elevations onto the mesh nodes (rounded to 4 decimals).
# ---------------------------------------------------------------------------
mesh = hydromate.interpolate_elevations(mesh, initial_clip, decimals=4)
print(f"elevations interpolated from {initial_clip.name} "
      f"(bottom {mesh.bottom.min():.4f}..{mesh.bottom.max():.4f} m)")

# ---------------------------------------------------------------------------
# Interpolate roughness onto the nodes from the roughness zones + table, so the
# mesh carries a roughness value (BOTTOM FRICTION) alongside the elevation. The
# table maps each zone id to an initial ks that HydroBayesCal later calibrates.
# ---------------------------------------------------------------------------
mesh = hydromate.interpolate_roughness(cfg, mesh)
print(f"roughness interpolated from {cfg.inputs.roughness_zones.name} "
      f"(ks {mesh.roughness.min():.3f}..{mesh.roughness.max():.3f} m)")

# store the mesh holding both elevation and roughness
elev_slf = hydromate.write_mesh(mesh, cfg.work_path("mesh-elevations.slf"),
                                title=f"{cfg.name} mesh with bathymetry + roughness")
print(f"mesh with elevations + roughness -> {elev_slf}")

# ---------------------------------------------------------------------------
# Compile the field ground truth into the tidy, multi-tab calibration table
# (one tab per category; first three columns x,y,z, then the measured
# quantities). For this Inn showcase the table is *generated* from the raw
# FlowTracker sources declared under inputs.ground_truth; in your own project
# you may instead hand-author this xlsx and point inputs.measurements at it.
# ---------------------------------------------------------------------------
ground_truth = hydromate.compile_ground_truth(cfg)
if ground_truth is not None:
    tables = hydromate.read_tidy(ground_truth)
    cats = ", ".join(f"{cat} ({len(df)} pts)" for cat, df in tables.items())
    print(f"ground truth compiled -> {ground_truth}  [{cats}]")
else:
    print("no ground_truth sources configured; provide a tidy inputs.measurements table")

print("preprocessing done.")
