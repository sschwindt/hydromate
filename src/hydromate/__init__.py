"""hydromate - automated setup of calibration-ready river models.

hydromate turns user-provided geodata and hydraulic data (DEM, ROI boundary,
inflow/outflow, measurements) into a production-ready simulation case, plus the
artifacts a surrogate-assisted Bayesian calibration needs. Two simulation backends
ship with it - TELEMAC-2D/3D (+GAIA) and OpenFOAM ``interFoam`` - and both are driven
from **one** case configuration describing the river and the modelling intent.

Layering
--------
* :mod:`hydromate.core` - solver-agnostic: the configuration, the geodata, the
  boundary conditions, the ground truth, the convergence maths, and the
  capability/registry machinery. **Nothing here imports a solver.**
* :mod:`hydromate.solvers` - one subpackage per simulation code, each supplying only
  what is genuinely its own and taking the rest from the core.

Everything is driven by a single YAML config (:mod:`hydromate.config`); what a given
case can actually do is recorded in its ``MODEL=<SOLVER>_<ENABLED|DISABLED>`` marker
files (:mod:`hydromate.core.capabilities`).

Lazy imports
------------
Attribute access is resolved on demand (PEP 562), so ``import hydromate`` costs
almost nothing and only the modules actually used are loaded. This is not
micro-optimisation: asking *what can this case do?* must not drag in numpy, pandas,
gmsh or rasterio, because that question gets asked far more often than "now go and
mesh it" - and a future QGIS plugin will ask it inside the QGIS Python process. The
public API is unchanged; ``from hydromate import build_mesh`` works exactly as before.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "0.1.0"

# Public name -> defining module. Grouped by module so the mapping stays legible and
# a new export is one word in the right tuple.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "core.capabilities": ("Capability", "CapabilityState", "CaseStatus",
                          "SolverStatus", "Support", "read_marker"),
    "core.registry": ("BackendSpec", "CapabilitySpec", "SolverBackend", "backends",
                      "supporting"),
    "config": ("Config", "load_config"),
    "dem": ("clip_dem_to_roi", "clip_to_roi", "dem_of_difference", "propagated_lod",
            "resolve_lod"),
    "ground_truth": ("compile_ground_truth", "read_tidy"),
    "targets": ("read_target_parameters", "read_targets", "write_target_template"),
    "flowtracker": ("fill_template_hydraulics", "read_flowtracker", "read_flowtrackers"),
    "mesh": ("build_mesh", "channel_node_mask", "interpolate_elevations",
             "interpolate_roughness", "write_mesh"),
    "mesh_quality": ("assess_quality",),
    "mesh_validity": ("MeshValidity", "channel_ks", "check_level"),
    "steering": ("select_turbulence_model", "eddy_viscosity_estimate"),
    "threed": ("build_3d_cas", "build_3d_cases", "infer_vertical_layers",
               "select_3d_turbulence"),
    "vertical_convergence": ("layer_levels", "run_vertical_convergence"),
    "rating": ("generate_stage_discharge", "normal_depth", "section_rating",
               "stage_for_discharge", "synthesize_outflow_rating",
               "synthesize_outflow_rating_from_section"),
    "sections": ("line_discharges", "write_line_discharges"),
    "wetting": ("outlet_profile", "OutletProfile", "wetting_report", "WettingReport"),
    "watertable": ("fit_phreatic_plane", "patch_node_mask", "PhreaticPlane",
                   "water_table_depth"),
    "convergence": ("percent_levels", "ratio_levels", "run_mesh_convergence"),
    "unsteady": ("build_unsteady_case", "build_unsteady_3d_case", "load_hydrograph",
                 "write_control_sections"),
    "flux_convergence": ("analyze_flux_convergence", "convergence_index",
                         "convergence_rate", "FluxConvergence", "relative_imbalance"),
    "sortie": ("find_lines", "latest_sortie", "read_sortie", "sediment_mass_profile",
               "Sortie", "tracer_mass_profile"),
    "workflow": ("format_3d_cases", "format_flux_convergence", "mesh_from_geometry",
                 "prepare_steady_inputs", "report_sections", "report_wetting",
                 "resolve_discharge", "synthesize_constant_inflow",
                 "synthesize_rating_if_missing", "run_solver_streaming",
                 "expected_duration", "water_table_mask"),
    "progress": ("SolverProgress", "ProgressBar"),
    "logsetup": ("setup_logging", "log_step", "logging_to"),
    "bayescal": ("FlowSpec", "run_single_flow_calibration",
                 "run_multiflow_calibration", "build_velocity_csv", "require_hbc"),
}

# submodules reachable as attributes without an explicit import
_SUBMODULES = ("core", "solvers", "campaigns", "openfoam", "config", "pipeline", "cli")

_NAME_TO_MODULE = {name: module
                   for module, names in _EXPORTS.items()
                   for name in names}

# Spelled out rather than computed from _EXPORTS: linters, type checkers and IDEs
# read __all__ statically, and with lazy attribute access it is the only thing that
# tells them these names exist. tests/test_capabilities.py asserts it stays in step
# with _EXPORTS, so the duplication cannot drift.
__all__ = [
    "BackendSpec", "Capability", "CapabilitySpec", "CapabilityState",
    "CaseStatus", "Config", "FlowSpec", "FluxConvergence", "MeshValidity",
    "OutletProfile", "PhreaticPlane", "ProgressBar", "SolverBackend",
    "SolverProgress", "SolverStatus", "Sortie", "Support", "WettingReport",
    "analyze_flux_convergence", "assess_quality", "backends", "build_3d_cas",
    "build_3d_cases", "build_mesh", "build_unsteady_3d_case", "build_unsteady_case",
    "build_velocity_csv", "channel_ks", "channel_node_mask", "check_level",
    "clip_dem_to_roi", "clip_to_roi", "compile_ground_truth", "convergence_index",
    "convergence_rate", "dem_of_difference", "eddy_viscosity_estimate",
    "expected_duration", "fill_template_hydraulics", "find_lines",
    "fit_phreatic_plane", "format_3d_cases", "format_flux_convergence",
    "generate_stage_discharge", "infer_vertical_layers", "interpolate_elevations",
    "interpolate_roughness", "latest_sortie", "layer_levels", "line_discharges",
    "load_config", "load_hydrograph", "log_step", "logging_to",
    "mesh_from_geometry", "normal_depth", "outlet_profile", "patch_node_mask",
    "percent_levels", "prepare_steady_inputs", "propagated_lod", "ratio_levels",
    "read_flowtracker", "read_flowtrackers", "read_marker", "read_sortie",
    "read_target_parameters", "read_targets", "read_tidy", "relative_imbalance",
    "report_sections", "report_wetting", "require_hbc", "resolve_discharge",
    "resolve_lod", "run_mesh_convergence", "run_multiflow_calibration",
    "run_single_flow_calibration", "run_solver_streaming",
    "run_vertical_convergence", "section_rating", "sediment_mass_profile",
    "select_3d_turbulence", "select_turbulence_model", "setup_logging",
    "stage_for_discharge", "supporting", "synthesize_constant_inflow",
    "synthesize_outflow_rating", "synthesize_outflow_rating_from_section",
    "synthesize_rating_if_missing", "tracer_mass_profile", "water_table_depth",
    "water_table_mask", "wetting_report", "write_control_sections",
    "write_line_discharges", "write_mesh", "write_target_template", "campaigns",
    "cli", "config", "core", "openfoam", "pipeline", "solvers", "__version__",
]


def __getattr__(name: str):
    """Resolve a public name (PEP 562) by importing only the module that defines it."""
    module = _NAME_TO_MODULE.get(name)
    if module is not None:
        return getattr(importlib.import_module(f"hydromate.{module}"), name)
    if name in _SUBMODULES:
        return importlib.import_module(f"hydromate.{name}")
    raise AttributeError(f"module 'hydromate' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # give type checkers and IDEs the real names
    from hydromate.bayescal import (
        build_velocity_csv, FlowSpec, require_hbc, run_multiflow_calibration,
        run_single_flow_calibration,
    )
    from hydromate.config import Config, load_config
    from hydromate.convergence import (
        percent_levels, ratio_levels, run_mesh_convergence,
    )
    from hydromate.core.capabilities import (
        Capability, CapabilityState, CaseStatus, read_marker, SolverStatus, Support,
    )
    from hydromate.core.registry import (
        backends, BackendSpec, CapabilitySpec, SolverBackend, supporting,
    )
    from hydromate.dem import (
        clip_dem_to_roi, clip_to_roi, dem_of_difference, propagated_lod, resolve_lod,
    )
    from hydromate.flowtracker import (
        fill_template_hydraulics, read_flowtracker, read_flowtrackers,
    )
    from hydromate.flux_convergence import (
        analyze_flux_convergence, convergence_index, convergence_rate,
        FluxConvergence, relative_imbalance,
    )
    from hydromate.ground_truth import compile_ground_truth, read_tidy
    from hydromate.logsetup import log_step, logging_to, setup_logging
    from hydromate.mesh import (
        build_mesh, channel_node_mask, interpolate_elevations, interpolate_roughness,
        write_mesh,
    )
    from hydromate.mesh_quality import assess_quality
    from hydromate.mesh_validity import channel_ks, check_level, MeshValidity
    from hydromate.progress import ProgressBar, SolverProgress
    from hydromate.rating import (
        generate_stage_discharge, normal_depth, section_rating, stage_for_discharge,
        synthesize_outflow_rating, synthesize_outflow_rating_from_section,
    )
    from hydromate.sections import line_discharges, write_line_discharges
    from hydromate.sortie import (
        find_lines, latest_sortie, read_sortie, sediment_mass_profile, Sortie,
        tracer_mass_profile,
    )
    from hydromate.steering import eddy_viscosity_estimate, select_turbulence_model
    from hydromate.targets import (
        read_target_parameters, read_targets, write_target_template,
    )
    from hydromate.threed import (
        build_3d_cas, build_3d_cases, infer_vertical_layers, select_3d_turbulence,
    )
    from hydromate.unsteady import (
        build_unsteady_3d_case, build_unsteady_case, load_hydrograph,
        write_control_sections,
    )
    from hydromate.vertical_convergence import layer_levels, run_vertical_convergence
    from hydromate.watertable import (
        fit_phreatic_plane, patch_node_mask, PhreaticPlane, water_table_depth,
    )
    from hydromate.wetting import (
        outlet_profile, OutletProfile, wetting_report, WettingReport,
    )
    from hydromate.workflow import (
        expected_duration, format_3d_cases, format_flux_convergence,
        mesh_from_geometry, prepare_steady_inputs, report_sections, report_wetting,
        resolve_discharge, run_solver_streaming, synthesize_constant_inflow,
        synthesize_rating_if_missing, water_table_mask,
    )
