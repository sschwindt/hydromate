"""Solver-agnostic core of axqua.

Everything here describes the *river and the modelling intent*, never a particular
simulation code: the case configuration, the geodata, the boundary conditions, the
ground truth, the convergence maths, and the capability/registry machinery that lets a
solver backend plug in.

The rule this package exists to enforce: **nothing in ``axqua.core`` imports a
solver.** Backends depend on core; core never depends on a backend. That is what makes
a second (or third) simulation code an addition rather than a rewrite.

:mod:`axqua.core.capabilities` and :mod:`axqua.core.registry` carry a stricter
rule still - standard library only - so that asking what a case can do never drags in
gmsh, rasterio or a solver's Python.
"""

from axqua.core.capabilities import (
    Capability, CapabilityState, CaseStatus, SolverStatus, Support, read_marker,
)
from axqua.core.registry import (
    BackendSpec, BaseBackend, CapabilitySpec, SolverBackend, backends, get, register,
    supporting,
)

__all__ = [
    "Capability", "CapabilityState", "CaseStatus", "SolverStatus", "Support",
    "read_marker",
    "BackendSpec", "BaseBackend", "CapabilitySpec", "SolverBackend", "backends",
    "get", "register",
    "supporting",
]
