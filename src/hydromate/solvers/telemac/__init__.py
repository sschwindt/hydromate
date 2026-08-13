"""TELEMAC-2D / TELEMAC-3D (+ GAIA) backend.

Everything specific to TELEMAC: the SERAFIN geometry writer's caller, the gmsh
meshing, the ``.cli`` boundary codes, the steering (``.cas``) writers for the steady,
unsteady and 3D runs, the GAIA coupling, the user-Fortran generation for a gain-lose
reach, the listing parser and the flux-convergence analysis, and the build pipeline
that ties them together.

What is deliberately *not* here: the configuration, the geodata, the boundary-line
reading, the ground truth, the rating curves and the SERAFIN codec itself. Those are
:mod:`hydromate.core`, shared with every other backend, which is what lets a second
solver be built from the same case description.

Attribute access is **lazy** (PEP 562), for the same reason as the OpenFOAM backend:
:mod:`~hydromate.solvers.telemac.spec` is read by ``hydromate status`` to build a
capability table and must not drag in gmsh or numpy to answer. Eager imports in this
``__init__`` would defeat that, since importing the spec runs it first.
"""

from __future__ import annotations

import importlib

_SUBMODULES = (
    "boundary", "flux_convergence", "fortran", "gainlose", "mesh", "mesh_quality",
    "pipeline", "sections", "sortie", "spec", "steering", "threed", "unsteady",
    "watertable", "wetting",
)

__all__ = [*_SUBMODULES]


def __getattr__(name: str):
    if name in _SUBMODULES:
        return importlib.import_module(f"hydromate.solvers.telemac.{name}")
    raise AttributeError(f"module 'hydromate.solvers.telemac' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
