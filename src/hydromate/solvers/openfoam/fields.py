"""The ``0/`` directory: initial and boundary conditions for the interFoam case.

Boundary conditions
-------------------
Every type here was checked against the OpenFOAM 9 source and the
``interFoam/RAS/{waterChannel,weirOverflow}`` tutorials, because the difference
between a river case that runs and one that oscillates is almost entirely in this
table.

============ ================================= ============================== ==================
patch        ``alpha.water``                   ``U``                          ``p_rgh``
============ ================================= ============================== ==================
inlet        ``variableHeightFlowRate``        ``variableHeightFlowRate\\
                                                InletVelocity``               ``fixedFluxPressure``
outlet       ``inletOutlet`` at the stage      ``pressureInletOutletVelocity`` ``prghPressure``
bed / banks  ``zeroGradient``                  ``noSlip``                     ``fixedFluxPressure``
atmosphere   ``inletOutlet`` (0)               ``pressureInletOutletVelocity`` ``totalPressure`` 0
============ ================================= ============================== ==================

Two of those deserve their reasoning recorded.

**The inlet does not prescribe a water level.** ``variableHeightFlowRateInletVelocity``
distributes the prescribed discharge over whatever part of the inlet is *wet*, and
``variableHeightFlowRate`` lets ``alpha`` at the inlet float between 0 and 1. So the
inlet stage is an outcome, not an input - which is what you want, because forcing
both Q and the depth at an inlet over-determines it and shows up as a standing wave
a few cells downstream.

**The outlet holds a stage.** The tutorials use a free outfall (``zeroGradient``
everywhere), which lets the model choose its own tailwater. This case has a
calibrated one - the same rating curve the TELEMAC run used - so the outlet gets
``prghPressure`` with a hydrostatic ``p`` profile below the target level and an
``inletOutlet`` ``alpha`` profile that is 1 below it and 0 above. That is the direct
3D analogue of TELEMAC's ``PRESCRIBED ELEVATIONS``, so 2D and 3D are driven
identically and their results are comparable. ``boundaries.outflow_condition: free``
still selects the free outfall.

Initial conditions
------------------
``alpha.water`` and ``U`` come from the 2D hotstart (see
:mod:`hydromate.solvers.openfoam.hotstart`) and are the only non-uniform internal fields;
``p_rgh`` starts at 0 (as in every tutorial - the first pressure solve resolves it)
and the turbulence fields start uniform at scales derived from the reach's own depth
and velocity.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import numpy as np

from hydromate.solvers.openfoam.polymesh import foam_footer, foam_header

log = logging.getLogger("hydromate")

GRAVITY = 9.81
C_MU = 0.09
TURBULENT_INTENSITY = 0.1     # 10%: open-channel flow over a rough bed
MIXING_LENGTH_FACTOR = 0.07   # L = 0.07 * depth, the standard hydraulic-diameter rule


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #


def scalar_list(values: np.ndarray) -> str:
    """``nonuniform List<scalar>`` body."""
    values = np.asarray(values, dtype=float)
    buf = io.StringIO()
    np.savetxt(buf, values, fmt="%.6g")
    return f"nonuniform List<scalar>\n{values.size}\n(\n{buf.getvalue()})"


def vector_list(values: np.ndarray) -> str:
    """``nonuniform List<vector>`` body."""
    values = np.asarray(values, dtype=float).reshape(-1, 3)
    buf = io.StringIO()
    np.savetxt(buf, values, fmt="(%.6g %.6g %.6g)")
    return f"nonuniform List<vector>\n{values.shape[0]}\n(\n{buf.getvalue()})"


def render_field(cls: str, obj: str, dimensions: str, internal: str,
                 boundary: dict[str, dict[str, str]], *, time_dir: str = "0") -> str:
    """Assemble one OpenFOAM field file."""
    out = [foam_header(cls, obj, location=time_dir)]
    out.append(f"dimensions      {dimensions};\n\n")
    out.append(f"internalField   {internal};\n\nboundaryField\n{{\n")
    for name, entries in boundary.items():
        out.append(f"    {name}\n    {{\n")
        for key, value in entries.items():
            out.append(f"        {key:<15} {value};\n")
        out.append("    }\n")
    out.append("}\n")
    out.append(foam_footer())
    return "".join(out)


# --------------------------------------------------------------------------- #
# the physical initial state
# --------------------------------------------------------------------------- #


def initial_alpha(of_mesh) -> np.ndarray:
    """Per-cell water fraction from the hotstart free surface.

    The submerged fraction of each cell, so the interface is sharp to within one
    cell wherever the 2D surface crosses a cell. With no hotstart the domain starts
    dry, which is legitimate but means paying for the whole filling transient in the
    most expensive model of the chain.
    """
    if of_mesh.column_wse is None:
        return np.zeros(of_mesh.n_cells)
    z_lo, z_hi = of_mesh.cell_bounds
    wse = np.asarray(of_mesh.column_wse, dtype=float)[of_mesh.cell_column]
    wse = np.where(np.isfinite(wse), wse, -np.inf)
    thickness = np.maximum(z_hi - z_lo, 1e-9)
    return np.clip((wse - z_lo) / thickness, 0.0, 1.0)


def initial_velocity(of_mesh, alpha: np.ndarray) -> np.ndarray:
    """Per-cell velocity: the 2D depth-averaged value in water, zero in air.

    Deliberately uniform over the depth rather than a log profile. The log profile's
    normalisation is only meaningful where the wall function is admissible, and on a
    gravel bed that is often nowhere (see :mod:`hydromate.solvers.openfoam.quality`); the
    vertical structure is what the 3D run is *for*, so it is left to develop during
    the spin-up stage rather than assumed here. Scaling by ``alpha`` keeps the
    interface cell from carrying a full-strength velocity into the air.
    """
    out = np.zeros((of_mesh.n_cells, 3))
    if of_mesh.column_uv is None:
        return out
    uv = np.asarray(of_mesh.column_uv, dtype=float)[of_mesh.cell_column]
    out[:, :2] = np.where(np.isfinite(uv), uv, 0.0) * alpha[:, None]
    return out


def turbulence_scales(state, cfg) -> tuple[float, float, float]:
    """``(k, epsilon, omega)`` seeds from the reach's own depth and velocity."""
    velocity = state.velocity_scale() if state is not None else \
        cfg.hydrodynamics.initial_velocity_guess
    depth = state.depth_scale() if state is not None else 1.0
    k = max(1.5 * (TURBULENT_INTENSITY * velocity) ** 2, 1e-6)
    length = max(MIXING_LENGTH_FACTOR * depth, 1e-3)
    epsilon = C_MU ** 0.75 * k ** 1.5 / length
    omega = k ** 0.5 / (C_MU ** 0.25 * length)
    return k, epsilon, omega


# --------------------------------------------------------------------------- #
# the field files
# --------------------------------------------------------------------------- #


def _wall_entries(of_mesh, cfg, patch: str) -> dict[str, str]:
    """``nut`` entry for a wall patch, with per-face ``Ks`` on the bed."""
    of = cfg.openfoam
    if patch == "bed" and of_mesh.bed_ks is not None:
        ks = np.nan_to_num(np.asarray(of_mesh.bed_ks, dtype=float),
                           nan=of.friction_ks)
        return {"type": "nutkRoughWallFunction",
                "Ks": scalar_list(ks),
                "Cs": f"uniform {of.roughness_constant:g}",
                "value": "uniform 0"}
    return {"type": "nutkRoughWallFunction",
            "Ks": f"uniform {of.friction_ks:g}",
            "Cs": f"uniform {of.roughness_constant:g}",
            "value": "uniform 0"}


def _outlet_profiles(of_mesh, patch: str, stage: float,
                     rho: float) -> tuple[str, str]:
    """``(alpha inletValue, p profile)`` on an outlet patch holding *stage*.

    ``alpha`` is 1 on faces whose centre lies below the target water level and 0
    above; ``p`` is the still-water hydrostatic pressure of a column standing at that
    level. Together they are what ``prghPressure`` needs to hold a tailwater.
    """
    ids = of_mesh.polymesh.patch_face_ids(patch)
    z = of_mesh.polymesh.face_centres(ids)[:, 2]
    alpha = (z <= stage).astype(float)
    pressure = rho * GRAVITY * np.maximum(stage - z, 0.0)
    return scalar_list(alpha), scalar_list(pressure)


def write_fields(of_mesh, cfg, case_dir: str | Path, *, state=None,
                 outflow_stage: float | None = None,
                 discharges: dict[str, float] | None = None) -> list[Path]:
    """Write ``0/{alpha.water, U, p_rgh, nut, k, omega|epsilon}``.

    *outflow_stage* is the water level [m a.s.l.] the outlet patches hold; ``None``
    selects the free-outfall boundary instead (``outflow_condition: free``).
    *discharges* maps each inlet patch to its own Q [m3/s].
    """
    of = cfg.openfoam
    case_dir = Path(case_dir)
    zero = case_dir / "0"
    zero.mkdir(parents=True, exist_ok=True)
    discharges = discharges or {}
    walls = ["bed", "banks"]
    written: list[Path] = []

    alpha = initial_alpha(of_mesh)
    velocity = initial_velocity(of_mesh, alpha)
    k, epsilon, omega = turbulence_scales(state, cfg)

    # ---- alpha.water --------------------------------------------------------
    bc: dict[str, dict[str, str]] = {}
    for patch in of_mesh.inlet_patches:
        # let the inlet stage settle itself; prescribing Q *and* a depth
        # over-determines the boundary
        bc[patch] = {"type": "variableHeightFlowRate", "lowerBound": "0",
                     "upperBound": "1", "value": "uniform 0"}
    for patch in of_mesh.outlet_patches:
        if outflow_stage is None:
            bc[patch] = {"type": "zeroGradient"}
        else:
            inlet_value, _ = _outlet_profiles(of_mesh, patch, outflow_stage,
                                              of.water_density)
            bc[patch] = {"type": "inletOutlet", "inletValue": inlet_value,
                         "value": inlet_value}
    for patch in walls:
        bc[patch] = {"type": "zeroGradient"}
    bc["atmosphere"] = {"type": "inletOutlet", "inletValue": "uniform 0",
                        "value": "uniform 0"}
    written.append(_write(zero / "alpha.water", render_field(
        "volScalarField", "alpha.water", "[0 0 0 0 0 0 0]",
        scalar_list(alpha), bc)))

    # ---- U ------------------------------------------------------------------
    bc = {}
    for patch in of_mesh.inlet_patches:
        q = discharges.get(patch)
        if q is None:
            raise ValueError(
                f"no discharge for inlet patch {patch!r}. Give each inflow line a "
                "'Target flow' value in boundaries.liquid_boundaries, or set "
                "boundaries.prescribed_flowrate for a single inflow.")
        bc[patch] = {"type": "variableHeightFlowRateInletVelocity",
                     "flowRate": f"{q:g}", "alpha": "alpha.water",
                     "value": "uniform (0 0 0)"}
    for patch in of_mesh.outlet_patches:
        bc[patch] = ({"type": "zeroGradient"} if outflow_stage is None else
                     {"type": "pressureInletOutletVelocity",
                      "value": "uniform (0 0 0)"})
    for patch in walls:
        bc[patch] = {"type": "noSlip"}
    bc["atmosphere"] = {"type": "pressureInletOutletVelocity",
                        "value": "uniform (0 0 0)"}
    written.append(_write(zero / "U", render_field(
        "volVectorField", "U", "[0 1 -1 0 0 0 0]", vector_list(velocity), bc)))

    # ---- p_rgh --------------------------------------------------------------
    bc = {}
    for patch in of_mesh.inlet_patches:
        bc[patch] = {"type": "fixedFluxPressure", "value": "uniform 0"}
    for patch in of_mesh.outlet_patches:
        if outflow_stage is None:
            bc[patch] = {"type": "zeroGradient"}
        else:
            _, profile = _outlet_profiles(of_mesh, patch, outflow_stage,
                                          of.water_density)
            bc[patch] = {"type": "prghPressure", "p": profile, "value": "uniform 0"}
    for patch in walls:
        bc[patch] = {"type": "fixedFluxPressure", "value": "uniform 0"}
    bc["atmosphere"] = {"type": "totalPressure", "p0": "uniform 0"}
    written.append(_write(zero / "p_rgh", render_field(
        "volScalarField", "p_rgh", "[1 -1 -2 0 0 0 0]", "uniform 0", bc)))

    if of.turbulence == "laminar":
        return written

    # ---- nut ----------------------------------------------------------------
    bc = {p: {"type": "calculated", "value": "uniform 0"}
          for p in of_mesh.inlet_patches + of_mesh.outlet_patches + ["atmosphere"]}
    for patch in walls:
        bc[patch] = _wall_entries(of_mesh, cfg, patch)
    written.append(_write(zero / "nut", render_field(
        "volScalarField", "nut", "[0 2 -1 0 0 0 0]", "uniform 0", bc)))

    # ---- k ------------------------------------------------------------------
    bc = {}
    for patch in of_mesh.inlet_patches:
        bc[patch] = {"type": "turbulentIntensityKineticEnergyInlet",
                     "intensity": f"{TURBULENT_INTENSITY:g}",
                     "value": f"uniform {k:.6g}"}
    for patch in of_mesh.outlet_patches + ["atmosphere"]:
        bc[patch] = {"type": "inletOutlet", "inletValue": f"uniform {k:.6g}",
                     "value": f"uniform {k:.6g}"}
    for patch in walls:
        bc[patch] = {"type": "kqRWallFunction", "value": f"uniform {k:.6g}"}
    written.append(_write(zero / "k", render_field(
        "volScalarField", "k", "[0 2 -2 0 0 0 0]", f"uniform {k:.6g}", bc)))

    # ---- omega / epsilon ----------------------------------------------------
    if of.turbulence == "kOmegaSST":
        name, value, dims, wall = "omega", omega, "[0 0 -1 0 0 0 0]", "omegaWallFunction"
    else:
        name, value, dims, wall = ("epsilon", epsilon, "[0 2 -3 0 0 0 0]",
                                   "epsilonWallFunction")
    bc = {}
    for patch in of_mesh.inlet_patches + of_mesh.outlet_patches + ["atmosphere"]:
        bc[patch] = {"type": "inletOutlet", "inletValue": f"uniform {value:.6g}",
                     "value": f"uniform {value:.6g}"}
    for patch in walls:
        bc[patch] = {"type": wall, "value": f"uniform {value:.6g}"}
    written.append(_write(zero / name, render_field(
        "volScalarField", name, dims, f"uniform {value:.6g}", bc)))

    wet = float((alpha > 0.5).mean())
    log.info("0/ fields written: %.1f%% of cells start as water, "
             "k=%.4g m2/s2, %s=%.4g", 100 * wet, k, name, value)
    return written


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path
