"""Compatibility shim: the OpenFOAM backend now lives at
:mod:`axqua.solvers.openfoam`.

Kept because ``axqua.openfoam`` was the published import path before the backends
were separated from the solver-agnostic core, and case scripts written against it are
sitting in users' working trees.

The alias is done by replacing this module in ``sys.modules`` rather than by
re-exporting names, so ``axqua.openfoam`` and ``axqua.solvers.openfoam`` are
**the same module object**. Re-exporting would have quietly dropped every private name
and broken identity checks; this way there is exactly one module and no second surface
to keep in step.
"""

from __future__ import annotations

import sys

from axqua.solvers import openfoam as _impl

# CPython's loader re-reads sys.modules[name] after executing a module body, so this
# substitution is honoured by `import axqua.openfoam` and by
# `from axqua.openfoam import x` alike.
sys.modules[__name__] = _impl
