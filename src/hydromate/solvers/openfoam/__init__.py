"""OpenFOAM ``interFoam`` free-surface backend.

Builds a 3D two-phase (VOF) case from the same configuration, geodata and converged
2D result as the TELEMAC case, into ``<sim_dir>/openfoam/``. Use it when the vertical
structure of the flow matters *and* the water surface has a gradient - the second
half is why ``simpleFoam`` is not an option, and why a two-phase solver (with its air
phase) is unavoidable.

Three design points carry most of the value:

* **The mesh is extruded, not snapped.** A river bed is a single-valued height field,
  so it is *followed* by a structured, terrain-following, all-hexahedral lattice
  written straight to ``constant/polyMesh`` - see
  :mod:`~hydromate.solvers.openfoam.polymesh` for why that avoids snappyHexMesh's
  failure modes entirely.
* **The lid follows the water.** Air is not modelled for its own sake, so the domain
  carries only ``freeboard`` metres of it above the free surface. This is the single
  biggest lever on interFoam's cost and stability.
* **The wall function constrains the mesh, not the other way round.** On a gravel bed
  ``ks`` is a large fraction of the depth, so the bed layer cannot be refined freely;
  :mod:`~hydromate.solvers.openfoam.quality` reports where that bites.

Attribute access is **lazy** (PEP 562). That is not decoration: this package's
sibling :mod:`~hydromate.solvers.openfoam.spec` is read by ``hydromate status`` and,
later, by a QGIS plugin to build a capability table, and importing it must not drag
in numpy, rasterio or the mesher. Eager imports here would defeat that, since
importing ``...openfoam.spec`` runs this ``__init__`` first.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_EXPORTS: dict[str, tuple[str, ...]] = {
    "polymesh": ("Patch", "PolyMesh", "write_polymesh"),
    "hotstart": ("State2D", "load_hotstart"),
    "mesh": ("OpenFoamMesh", "PlanGrid", "build_mesh", "build_plan_grid",
             "sigma_levels"),
    "quality": ("MeshReport", "assess"),
    "case": ("OpenFoamArtifacts", "build_case", "estimate_cells", "summarise"),
    "report": ("DischargeHistory", "analyse", "write_report"),
    "runtime": ("OpenFoamProgress", "OpenFoamRuntime"),
}
_SUBMODULES = ("case", "dicts", "fields", "hotstart", "mesh", "polymesh", "quality",
               "report", "runtime", "spec")

_NAME_TO_MODULE = {name: module
                   for module, names in _EXPORTS.items()
                   for name in names}

__all__ = [
    "DischargeHistory", "MeshReport", "OpenFoamArtifacts", "OpenFoamMesh",
    "OpenFoamProgress", "OpenFoamRuntime", "Patch", "PlanGrid", "PolyMesh",
    "State2D", "analyse", "assess", "build_case", "build_mesh", "build_plan_grid",
    "estimate_cells", "load_hotstart", "sigma_levels", "summarise", "write_polymesh",
    "write_report", *_SUBMODULES,
]


def __getattr__(name: str):
    module = _NAME_TO_MODULE.get(name)
    if module is not None:
        return getattr(importlib.import_module(
            f"hydromate.solvers.openfoam.{module}"), name)
    if name in _SUBMODULES:
        return importlib.import_module(f"hydromate.solvers.openfoam.{name}")
    raise AttributeError(
        f"module 'hydromate.solvers.openfoam' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # give type checkers and IDEs the real names
    from hydromate.solvers.openfoam.case import (
        build_case, estimate_cells, OpenFoamArtifacts, summarise,
    )
    from hydromate.solvers.openfoam.hotstart import load_hotstart, State2D
    from hydromate.solvers.openfoam.mesh import (
        build_mesh, build_plan_grid, OpenFoamMesh, PlanGrid, sigma_levels,
    )
    from hydromate.solvers.openfoam.polymesh import Patch, PolyMesh, write_polymesh
    from hydromate.solvers.openfoam.quality import assess, MeshReport
    from hydromate.solvers.openfoam.report import (
        analyse, DischargeHistory, write_report,
    )
    from hydromate.solvers.openfoam.runtime import OpenFoamProgress, OpenFoamRuntime
