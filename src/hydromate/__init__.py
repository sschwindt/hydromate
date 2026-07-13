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
from hydromate.dem import (
    clip_dem_to_roi, clip_to_roi, dem_of_difference, propagated_lod, resolve_lod,
)
from hydromate.ground_truth import compile_ground_truth, read_tidy
from hydromate.targets import (
    read_target_parameters, read_targets, write_target_template,
)
from hydromate.flowtracker import (
    fill_template_hydraulics, read_flowtracker, read_flowtrackers,
)
from hydromate.mesh import (
    build_mesh, channel_node_mask, interpolate_elevations, interpolate_roughness,
    write_mesh,
)
from hydromate.mesh_quality import assess_quality
from hydromate.steering import select_turbulence_model, eddy_viscosity_estimate
from hydromate.threed import (
    build_3d_cas, build_3d_cases, infer_vertical_layers, select_3d_turbulence,
)
from hydromate.vertical_convergence import (
    layer_levels, run_vertical_convergence,
)
from hydromate.rating import (
    generate_stage_discharge, normal_depth, synthesize_outflow_rating,
)
from hydromate.convergence import percent_levels, ratio_levels, run_mesh_convergence
from hydromate.mesh_validity import MeshValidity, channel_ks, check_level
from hydromate.unsteady import (
    build_unsteady_case, build_unsteady_3d_case, load_hydrograph,
    write_control_sections,
)
from hydromate.flux_convergence import analyze_flux_convergence, FluxConvergence
from hydromate.workflow import (
    format_3d_cases, format_flux_convergence, prepare_steady_inputs,
    resolve_discharge, synthesize_constant_inflow, synthesize_rating_if_missing,
    run_solver_streaming, expected_duration,
)
from hydromate.progress import SolverProgress
from hydromate.logsetup import setup_logging, log_step, logging_to
from hydromate import campaigns
from hydromate.bayescal import (
    FlowSpec, run_single_flow_calibration, run_multiflow_calibration,
    build_velocity_csv, hbc_dir, hbc_env,
)

__all__ = ["Config", "load_config", "clip_to_roi", "clip_dem_to_roi",
           "dem_of_difference", "propagated_lod", "resolve_lod",
           "compile_ground_truth", "read_tidy",
           "write_target_template", "read_targets", "read_target_parameters",
           "read_flowtracker", "read_flowtrackers", "fill_template_hydraulics",
           "build_mesh", "channel_node_mask", "interpolate_elevations",
           "interpolate_roughness", "write_mesh", "assess_quality",
           "select_turbulence_model", "eddy_viscosity_estimate",
           "build_3d_cas", "build_3d_cases", "infer_vertical_layers",
           "select_3d_turbulence",
           "layer_levels", "run_vertical_convergence",
           "generate_stage_discharge", "normal_depth", "synthesize_outflow_rating",
           "run_mesh_convergence", "percent_levels", "ratio_levels",
           "MeshValidity", "channel_ks", "check_level",
           "build_unsteady_case", "build_unsteady_3d_case", "load_hydrograph",
           "write_control_sections",
           "analyze_flux_convergence", "FluxConvergence",
           "format_3d_cases", "format_flux_convergence",
           "prepare_steady_inputs", "resolve_discharge",
           "synthesize_constant_inflow", "synthesize_rating_if_missing",
           "run_solver_streaming", "expected_duration", "SolverProgress",
           "setup_logging", "log_step", "logging_to",
           "campaigns", "FlowSpec", "run_single_flow_calibration",
           "run_multiflow_calibration", "build_velocity_csv", "hbc_dir", "hbc_env",
           "__version__"]
