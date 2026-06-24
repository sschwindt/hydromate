"""Minimal, self-contained SELAFIN (Serafin) writer for TELEMAC geometry files.

Single-precision, big-endian, Fortran-record layout following the de-facto
reference implementation (pputils ``ppSELAFIN``). Vectorised with numpy so it
scales to large meshes. We only need to *write* a 2D geometry file (one frame
holding the BOTTOM variable), which TELEMAC reads as the GEOMETRY FILE.

A SELAFIN record is ``<int32 nbytes><payload><int32 nbytes>`` (the integers are
Fortran unformatted record markers), everything big-endian.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np

_I4 = np.dtype(">i4")
_F4 = np.dtype(">f4")


def _record(payload: bytes) -> bytes:
    marker = np.array(len(payload), dtype=_I4).tobytes()
    return marker + payload + marker


def write_geometry(
    path: str | Path,
    x: np.ndarray,
    y: np.ndarray,
    ikle: np.ndarray,
    ipobo: np.ndarray,
    bottom: np.ndarray,
    *,
    friction_id: np.ndarray | None = None,
    title: str = "hydromate geometry",
    date: Sequence[int] = (2026, 1, 1, 0, 0, 0),
) -> Path:
    """Write a 2D triangular-mesh geometry SELAFIN.

    Always writes the BOTTOM variable; if *friction_id* is given it is written as
    a second variable ``FRIC_ID`` (per node), which TELEMAC reads to map nodes to
    friction zones in the FRICTION DATA FILE (no separate ZONES FILE needed).

    Parameters
    ----------
    x, y : (NPOIN,) float arrays of node coordinates.
    ikle : (NELEM, 3) int array of 1-based node indices per triangle.
    ipobo : (NPOIN,) int array — boundary numbering (0 interior, k>0 on contour).
    bottom : (NPOIN,) float array of bed elevations (m).
    friction_id : optional (NPOIN,) int array of friction-zone ids per node.
    """
    path = Path(path)
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    bottom = np.asarray(bottom, dtype=float)
    ikle = np.asarray(ikle, dtype=np.int64)
    ipobo = np.asarray(ipobo, dtype=np.int64)

    npoin = x.size
    nelem = ikle.shape[0]
    ndp = ikle.shape[1]
    if ndp != 3:
        raise ValueError(f"only triangular meshes supported (NDP=3), got {ndp}")
    for arr, name in ((y, "y"), (bottom, "bottom"), (ipobo, "ipobo")):
        if arr.size != npoin:
            raise ValueError(f"{name} has length {arr.size}, expected NPOIN={npoin}")

    variables = [("BOTTOM", "M", bottom)]
    if friction_id is not None:
        friction_id = np.asarray(friction_id, dtype=float)
        if friction_id.size != npoin:
            raise ValueError(f"friction_id length {friction_id.size} != NPOIN={npoin}")
        variables.append(("FRIC_ID", "", friction_id))
    nbv1 = len(variables)

    # IPARAM: index 9 == 1 -> a DATE record follows; index 6 == 0 -> 2D.
    iparam = np.zeros(10, dtype=_I4)
    iparam[0] = 1
    iparam[9] = 1

    with open(path, "wb") as f:
        # 1) title (72) + precision tag (8) = 80-byte record
        f.write(_record(f"{title:<72}".encode("ascii") + b"SERAFIN "))
        # 2) NBV1, NBV2
        f.write(_record(np.array([nbv1, 0], dtype=_I4).tobytes()))
        # 3) one 32-byte record per variable: name(16) + unit(16)
        for name, unit, _ in variables:
            f.write(_record(f"{name:<16}{unit:<16}".encode("ascii")))
        # 4) IPARAM (10 int)
        f.write(_record(iparam.tobytes()))
        # 5) DATE (6 int)
        f.write(_record(np.array(date, dtype=_I4).tobytes()))
        # 6) NELEM, NPOIN, NDP, NPLAN(=1)
        f.write(_record(np.array([nelem, npoin, ndp, 1], dtype=_I4).tobytes()))
        # 7) IKLE connectivity (row-major, 1-based)
        f.write(_record(ikle.astype(_I4).tobytes()))
        # 8) IPOBO
        f.write(_record(ipobo.astype(_I4).tobytes()))
        # 9) X, 10) Y
        f.write(_record(x.astype(_F4).tobytes()))
        f.write(_record(y.astype(_F4).tobytes()))
        # 11) one time frame: t=0 then each variable's NPOIN values
        f.write(_record(np.array([0.0], dtype=_F4).tobytes()))
        for _, _, values in variables:
            f.write(_record(np.asarray(values, dtype=float).astype(_F4).tobytes()))
    return path
