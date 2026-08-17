"""axqua - automated setup of calibration-ready river models.

axqua turns user-provided geodata and hydraulic data (DEM, ROI boundary,
inflow/outflow, measurements) into a production-ready simulation case, plus the
artifacts a surrogate-assisted Bayesian calibration needs. Two simulation backends
ship with it - TELEMAC-2D/3D (+GAIA) and OpenFOAM ``interFoam`` - and both are driven
from **one** case configuration describing the river and the modelling intent.

Layering
--------
* :mod:`axqua.core` - solver-agnostic: the configuration, the geodata, the
  boundary conditions, the ground truth, the convergence maths, and the
  capability/registry machinery. **Nothing here imports a solver.**
* :mod:`axqua.solvers` - one subpackage per simulation code, each supplying only
  what is genuinely its own and taking the rest from the core.

Everything is driven by a single YAML config (:mod:`axqua.config`); what a given
case can actually do is recorded in its ``MODEL=<SOLVER>_<ENABLED|DISABLED>`` marker
files (:mod:`axqua.core.capabilities`).

Lazy imports
------------
Attribute access is resolved on demand (PEP 562), so ``import axqua`` costs
almost nothing and only the modules actually used are loaded. This is not
micro-optimisation: asking *what can this case do?* must not drag in numpy, pandas,
gmsh or rasterio, because that question gets asked far more often than "now go and
mesh it" - and a future QGIS plugin will ask it inside the QGIS Python process. The
public API is unchanged; ``from axqua import build_mesh`` works exactly as before.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Single source of truth: whatever was installed. The literal is the fallback for a
# source checkout that was never `pip install`ed, and is kept in step with
# pyproject.toml by a test.
_FALLBACK_VERSION = "0.3.0"

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("aXqua")
except Exception:  # noqa: BLE001 - a checkout that was never installed
    __version__ = _FALLBACK_VERSION

# Public name -> defining module. Grouped by module so the mapping stays legible and
# a new export is one word in the right tuple.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "core.capabilities": ("Capability", "CapabilityState", "CaseStatus",
                          "SolverStatus", "Support", "read_marker"),
    "core.registry": ("BackendSpec", "CapabilitySpec", "SolverBackend", "backends",
                      "supporting"),
    "config": ("Config", "dump_config", "load_config"),
    "core.errors": ("ConfigError", "ErrorRecord", "GeodataError",
                    "AxquaError", "MeshError", "SolverError"),
    "core.schema": ("Layer", "classify_setting"),
    "dem": ("clip_dem_to_roi", "clip_to_roi", "dem_of_difference", "propagated_lod",
            "resolve_lod"),
    "ground_truth": ("compile_ground_truth", "read_tidy"),
    "targets": ("read_target_parameters", "read_targets", "write_target_template"),
    "flowtracker": ("fill_template_hydraulics", "read_flowtracker", "read_flowtrackers"),
    "solvers.telemac.mesh": ("build_mesh", "channel_node_mask", "interpolate_elevations",
             "interpolate_roughness", "write_mesh"),
    "solvers.telemac.mesh_quality": ("assess_quality",),
    "mesh_validity": ("MeshValidity", "channel_ks", "check_level"),
    "solvers.telemac.steering": ("select_turbulence_model", "eddy_viscosity_estimate"),
    "solvers.telemac.threed": ("build_3d_cas", "build_3d_cases", "infer_vertical_layers",
               "select_3d_turbulence"),
    "vertical_convergence": ("layer_levels", "run_vertical_convergence"),
    "rating": ("generate_stage_discharge", "normal_depth", "section_rating",
               "stage_for_discharge", "synthesize_outflow_rating",
               "synthesize_outflow_rating_from_section"),
    "solvers.telemac.sections": ("line_discharges", "write_line_discharges"),
    "solvers.telemac.wetting": ("outlet_profile", "OutletProfile", "wetting_report", "WettingReport"),
    "solvers.telemac.watertable": ("fit_phreatic_plane", "patch_node_mask", "PhreaticPlane",
                   "water_table_depth"),
    "convergence": ("percent_levels", "ratio_levels", "run_mesh_convergence"),
    "solvers.telemac.unsteady": ("build_unsteady_case", "build_unsteady_3d_case", "load_hydrograph",
                 "write_control_sections"),
    "solvers.telemac.flux_convergence": ("analyze_flux_convergence", "convergence_index",
                         "convergence_rate", "FluxConvergence", "relative_imbalance"),
    "solvers.telemac.sortie": ("find_lines", "latest_sortie", "read_sortie", "sediment_mass_profile",
               "Sortie", "tracer_mass_profile"),
    "workflow": ("format_3d_cases", "format_flux_convergence", "mesh_from_geometry",
                 "prepare_steady_inputs", "report_sections", "report_wetting",
                 "resolve_discharge", "synthesize_constant_inflow",
                 "synthesize_rating_if_missing", "run_solver_streaming",
                 "expected_duration", "water_table_mask"),
    "prerun": ("SeedResult", "ensure_seed"),
    "progress": ("SolverProgress", "ProgressBar"),
    "logsetup": ("setup_logging", "log_step", "logging_to"),
    "bayescal": ("FlowSpec", "run_single_flow_calibration",
                 "run_multiflow_calibration", "build_velocity_csv", "require_hbc"),
}

# submodules reachable as attributes without an explicit import
# Historic module names stay listed: they are shims (see axqua/mesh.py and
# friends) that alias the moved modules, so `from axqua import pipeline`
# keeps working for case scripts written before the backends were split out.
_SUBMODULES = ("core", "solvers", "campaigns", "openfoam", "config", "pipeline",
               "cli", "mesh", "steering", "selafin")

_NAME_TO_MODULE = {name: module
                   for module, names in _EXPORTS.items()
                   for name in names}

# Spelled out rather than computed from _EXPORTS: linters, type checkers and IDEs
# read __all__ statically, and with lazy attribute access it is the only thing that
# tells them these names exist. tests/test_capabilities.py asserts it stays in step
# with _EXPORTS, so the duplication cannot drift.
__all__ = [
    "BackendSpec", "Capability", "CapabilitySpec", "CapabilityState", "CaseStatus",
    "Config", "ConfigError", "ErrorRecord", "FlowSpec", "FluxConvergence",
    "GeodataError", "AxquaError", "Layer", "MeshError", "MeshValidity",
    "OutletProfile", "SolverError",
    "PhreaticPlane", "ProgressBar", "SolverBackend", "SolverProgress",
    "SeedResult", "SolverStatus", "Sortie", "Support", "WettingReport",
    "analyze_flux_convergence", "assess_quality", "backends", "build_3d_cas",
    "build_3d_cases", "build_mesh", "build_unsteady_3d_case", "build_unsteady_case",
    "build_velocity_csv", "channel_ks", "channel_node_mask", "check_level",
    "clip_dem_to_roi", "clip_to_roi", "compile_ground_truth", "convergence_index",
    "classify_setting", "convergence_rate", "dem_of_difference",
    "dump_config", "eddy_viscosity_estimate",
    "ensure_seed", "expected_duration", "fill_template_hydraulics", "find_lines",
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
    "cli", "config", "core", "mesh", "openfoam", "pipeline", "selafin", "solvers",
    "steering", "__version__",
]


def __getattr__(name: str):
    """Resolve a public name (PEP 562) by importing only the module that defines it."""
    module = _NAME_TO_MODULE.get(name)
    if module is not None:
        return getattr(importlib.import_module(f"axqua.{module}"), name)
    if name in _SUBMODULES:
        return importlib.import_module(f"axqua.{name}")
    raise AttributeError(f"module 'axqua' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # give type checkers and IDEs the real names
    from axqua.bayescal import (
        build_velocity_csv, FlowSpec, require_hbc, run_multiflow_calibration,
        run_single_flow_calibration,
    )
    from axqua.config import Config, load_config
    from axqua.convergence import (
        percent_levels, ratio_levels, run_mesh_convergence,
    )
    from axqua.core.capabilities import (
        Capability, CapabilityState, CaseStatus, read_marker, SolverStatus, Support,
    )
    from axqua.core.registry import (
        backends, BackendSpec, CapabilitySpec, SolverBackend, supporting,
    )
    from axqua.dem import (
        clip_dem_to_roi, clip_to_roi, dem_of_difference, propagated_lod, resolve_lod,
    )
    from axqua.flowtracker import (
        fill_template_hydraulics, read_flowtracker, read_flowtrackers,
    )
    from axqua.solvers.telemac.flux_convergence import (
        analyze_flux_convergence, convergence_index, convergence_rate,
        FluxConvergence, relative_imbalance,
    )
    from axqua.ground_truth import compile_ground_truth, read_tidy
    from axqua.logsetup import log_step, logging_to, setup_logging
    from axqua.solvers.telemac.mesh import (
        build_mesh, channel_node_mask, interpolate_elevations, interpolate_roughness,
        write_mesh,
    )
    from axqua.solvers.telemac.mesh_quality import assess_quality
    from axqua.mesh_validity import channel_ks, check_level, MeshValidity
    from axqua.progress import ProgressBar, SolverProgress
    from axqua.rating import (
        generate_stage_discharge, normal_depth, section_rating, stage_for_discharge,
        synthesize_outflow_rating, synthesize_outflow_rating_from_section,
    )
    from axqua.solvers.telemac.sections import line_discharges, write_line_discharges
    from axqua.solvers.telemac.sortie import (
        find_lines, latest_sortie, read_sortie, sediment_mass_profile, Sortie,
        tracer_mass_profile,
    )
    from axqua.solvers.telemac.steering import eddy_viscosity_estimate, select_turbulence_model
    from axqua.targets import (
        read_target_parameters, read_targets, write_target_template,
    )
    from axqua.solvers.telemac.threed import (
        build_3d_cas, build_3d_cases, infer_vertical_layers, select_3d_turbulence,
    )
    from axqua.solvers.telemac.unsteady import (
        build_unsteady_3d_case, build_unsteady_case, load_hydrograph,
        write_control_sections,
    )
    from axqua.vertical_convergence import layer_levels, run_vertical_convergence
    from axqua.solvers.telemac.watertable import (
        fit_phreatic_plane, patch_node_mask, PhreaticPlane, water_table_depth,
    )
    from axqua.solvers.telemac.wetting import (
        outlet_profile, OutletProfile, wetting_report, WettingReport,
    )
    from axqua.workflow import (
        expected_duration, format_3d_cases, format_flux_convergence,
        mesh_from_geometry, prepare_steady_inputs, report_sections, report_wetting,
        resolve_discharge, run_solver_streaming, synthesize_constant_inflow,
        synthesize_rating_if_missing, water_table_mask,
    )
