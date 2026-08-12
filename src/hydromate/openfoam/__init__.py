"""OpenFOAM free-surface (interFoam) extension for hydromate.

This subpackage is **additive**: it builds a 3D VOF case beside the TELEMAC case
from the same config, the same geodata and - crucially - the same converged 2D
result. Nothing in the TELEMAC path imports it, and a config with no ``openfoam:``
block never reaches it.

The workflow is one step further along the same line the TELEMAC path already
follows (2D build -> 2D run -> mesh convergence -> 3D):

1. ``preprocessing.py`` + ``initial_run.py`` produce a converged ``r2d.slf``;
2. ``openfoam_preprocessing.py`` turns it into a complete OpenFOAM case -
   :mod:`~hydromate.openfoam.mesh` writes a terrain-following all-hex
   ``constant/polyMesh``, :mod:`~hydromate.openfoam.hotstart` seeds the fields from
   the 2D state, :mod:`~hydromate.openfoam.dicts` writes the two staged dict sets;
3. ``openfoam_run.py`` runs ``checkMesh``, both stages of ``interFoam``, and the
   discharge-convergence report.

Three design points worth knowing before reading the code:

* **The mesh is extruded, not snapped.** A river bed is a single-valued height
  field, so it is followed rather than snapped to - see
  :mod:`hydromate.openfoam.polymesh` for why that avoids snappyHexMesh's failure
  modes entirely.
* **The lid follows the water.** Air is not modelled for its own sake, so the domain
  carries only ``freeboard`` metres of it above the free surface. This is the single
  biggest lever on interFoam's cost and stability.
* **The wall function constrains the mesh, not the other way round.** On a gravel
  bed ``ks`` is a large fraction of the depth, so the bed layer cannot be refined
  freely; :mod:`hydromate.openfoam.quality` reports where that bites.
"""

from __future__ import annotations

from hydromate.openfoam.polymesh import Patch, PolyMesh, write_polymesh
from hydromate.openfoam.hotstart import State2D, load_hotstart
from hydromate.openfoam.mesh import (
    OpenFoamMesh, PlanGrid, build_mesh, build_plan_grid, sigma_levels,
)
from hydromate.openfoam.quality import MeshReport, assess
from hydromate.openfoam.case import (
    OpenFoamArtifacts, build_case, estimate_cells, summarise,
)
from hydromate.openfoam.report import DischargeHistory, analyse, write_report
from hydromate.openfoam.runtime import OpenFoamProgress, OpenFoamRuntime
from hydromate.openfoam import dicts, fields

__all__ = [
    "Patch", "PolyMesh", "write_polymesh",
    "State2D", "load_hotstart",
    "OpenFoamMesh", "PlanGrid", "build_mesh", "build_plan_grid", "sigma_levels",
    "MeshReport", "assess",
    "OpenFoamArtifacts", "build_case", "estimate_cells", "summarise",
    "DischargeHistory", "analyse", "write_report",
    "OpenFoamRuntime", "OpenFoamProgress",
    "dicts", "fields",
]
