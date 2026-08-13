"""Compatibility shim: the SERAFIN codec now lives at :mod:`hydromate.core.selafin`.

It sits in the core rather than in the TELEMAC backend because it is a **file format**,
not a solver: TELEMAC writes its geometry through it, and the OpenFOAM backend reads a
TELEMAC result through it to build its hotstart. Neither owns it.

Aliased through ``sys.modules`` so this and the real module are one object.
"""

from __future__ import annotations

import sys

from hydromate.core import selafin as _impl

sys.modules[__name__] = _impl
