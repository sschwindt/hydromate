"""Compatibility shim: this module now lives at
:mod:`hydromate.solvers.telemac.boundary`.

Kept because ``hydromate.boundary`` was the published import path before the TELEMAC
backend was separated from the solver-agnostic core, and case scripts written against
it are sitting in users' working trees.

Aliased through ``sys.modules`` rather than by re-exporting names, so this and the
real module are **one object** - re-exporting would silently drop every private name
and break identity checks.
"""

from __future__ import annotations

import sys

from hydromate.solvers.telemac import boundary as _impl

sys.modules[__name__] = _impl
