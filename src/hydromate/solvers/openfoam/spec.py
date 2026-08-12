"""What the OpenFOAM backend can do, and how to tell what a case has done with it.

Import-light by contract (see :mod:`hydromate.solvers`): nothing here imports the
OpenFOAM extension's own modules, so a capability listing costs one small import.

The support matrix is where this backend differs most from TELEMAC's, and the
distinction between *not applicable* and *not implemented* carries real information:

* ``steady2d`` / ``unsteady2d`` are **not applicable**. ``interFoam`` is a two-phase
  VOF solver; there is no depth-averaged mode to run, and reporting "not implemented"
  would suggest hydromate is merely missing a feature that could be added.
* ``morphodynamics``, ``calibration`` and the convergence studies are **not
  implemented**: each is genuinely possible for OpenFOAM and simply is not built yet.
  A user deciding which code to set up deserves to see that difference.
"""

from __future__ import annotations

from pathlib import Path

from hydromate.core.capabilities import Capability, Support
from hydromate.core.registry import BackendSpec, CapabilitySpec


def _case(cfg) -> Path:
    return Path(cfg.openfoam_case_dir)


def _mesh_built(cfg) -> bool:
    """The mesh is the case: no ``faces`` file, nothing to run."""
    return (_case(cfg) / "constant" / "polyMesh" / "faces").is_file()


def _has_run(cfg) -> bool:
    """Any written time directory beyond ``0`` - in the case root after a serial run
    or a reconstruct, or inside ``processor0`` while a parallel run is still
    decomposed."""
    for root in (_case(cfg), _case(cfg) / "processor0"):
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            try:
                if float(entry.name) > 0.0:
                    return True
            except ValueError:
                continue
    return False


SPEC = BackendSpec(
    name="openfoam",
    title="OpenFOAM interFoam (two-phase free surface)",
    config_key="openfoam",
    implementation="hydromate.solvers.openfoam.backend:BACKEND",
    # Declared by the case when the block names an OpenFOAM installation. Whether
    # *this machine* has it is the marker body's `env` line, not the filename.
    enabled=lambda cfg: "openfoam" in cfg.declared_blocks,
    environment=lambda cfg: str(cfg.openfoam.bashrc or ""),
    capabilities={
        Capability.STEADY2D: CapabilitySpec(support=Support.NOT_APPLICABLE),
        Capability.UNSTEADY2D: CapabilitySpec(support=Support.NOT_APPLICABLE),
        # The VOF run is 3D and transient by nature; it is reported under
        # free_surface_3d rather than pretending to be a "steady 3D" case.
        Capability.STEADY3D: CapabilitySpec(support=Support.NOT_APPLICABLE),
        Capability.UNSTEADY3D: CapabilitySpec(support=Support.NOT_APPLICABLE),
        Capability.FREE_SURFACE_3D: CapabilitySpec(
            support=Support.SUPPORTED,
            configured=lambda cfg: "openfoam" in cfg.declared_blocks,
            built=_mesh_built,
            run=_has_run,
        ),
        # Possible, not yet built - see the plan's phase list.
        Capability.MORPHODYNAMICS: CapabilitySpec(support=Support.NOT_IMPLEMENTED),
        Capability.GAIN_LOSE: CapabilitySpec(support=Support.NOT_IMPLEMENTED),
        Capability.MESH_CONVERGENCE: CapabilitySpec(support=Support.NOT_IMPLEMENTED),
        Capability.VERTICAL_CONVERGENCE: CapabilitySpec(support=Support.NOT_IMPLEMENTED),
        Capability.CALIBRATION: CapabilitySpec(support=Support.NOT_IMPLEMENTED),
    },
)
