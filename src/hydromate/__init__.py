"""hydromate - automated setup of calibration-ready TELEMAC + GAIA cases.

The package turns user-provided geodata and hydraulic data (DEM, ROI boundary,
inflow/outflow, measurements) into a production-ready TELEMAC-2D (optionally
+GAIA) case plus a HydroBayesCal ``config_Telemac.py``, so a surrogate-assisted
Bayesian calibration can be launched with ``bal_telemac.py``.

Pipeline stages (see :mod:`hydromate.pipeline`):

1. DEM ingest + clip to the region of interest        (:mod:`hydromate.dem`)
2. mesh + bathymetry + SELAFIN geometry               (:mod:`hydromate.mesh`)
3. boundary conditions ``.cli``                       (:mod:`hydromate.boundary`)
4. steering ``.cas`` + friction ``.tbl``              (:mod:`hydromate.steering`)
5. calibration CSV + HydroBayesCal config emit        (:mod:`hydromate.calibration`)

Everything is driven by a single YAML config (:mod:`hydromate.config`).
"""

__version__ = "0.1.0"

from hydromate.config import Config, load_config
from hydromate.dem import clip_dem_to_roi, clip_to_roi
from hydromate.ground_truth import compile_ground_truth, read_tidy
from hydromate.mesh import (
    build_mesh, channel_node_mask, interpolate_elevations, interpolate_roughness,
    write_mesh,
)
from hydromate.mesh_quality import assess_quality
from hydromate.rating import (
    generate_stage_discharge, normal_depth, synthesize_outflow_rating,
)
from hydromate.convergence import percent_levels, run_mesh_convergence
from hydromate.flux_convergence import analyze_flux_convergence, FluxConvergence
from hydromate.logsetup import setup_logging, log_step, logging_to

__all__ = ["Config", "load_config", "clip_to_roi", "clip_dem_to_roi",
           "compile_ground_truth", "read_tidy",
           "build_mesh", "channel_node_mask", "interpolate_elevations",
           "interpolate_roughness", "write_mesh", "assess_quality",
           "generate_stage_discharge", "normal_depth", "synthesize_outflow_rating",
           "run_mesh_convergence", "percent_levels",
           "analyze_flux_convergence", "FluxConvergence",
           "setup_logging", "log_step", "logging_to", "__version__"]
