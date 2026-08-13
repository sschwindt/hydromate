"""Minimal, self-contained SELAFIN (Serafin) writer for TELEMAC geometry files.

Single-precision, big-endian, Fortran-record layout following the de-facto
reference implementation (pputils ``ppSELAFIN``). Vectorised with numpy so it
scales to large meshes. We only need to *write* a 2D geometry file (one frame
holding the BOTTOM variable), which TELEMAC reads as the GEOMETRY FILE.

A SELAFIN record is ``<int32 nbytes><payload><int32 nbytes>`` (the integers are
Fortran unformatted record markers), everything big-endian.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger("hydromate")

_I4 = np.dtype(">i4")
_F4 = np.dtype(">f4")
_F8 = np.dtype(">f8")


def _record(payload: bytes) -> bytes:
    marker = np.array(len(payload), dtype=_I4).tobytes()
    return marker + payload + marker


def _read_record(f) -> bytes | None:
    """Read one Fortran unformatted record; return its payload (None at EOF)."""
    head = f.read(4)
    if len(head) < 4:
        return None
    n = int(np.frombuffer(head, dtype=_I4)[0])
    payload = f.read(n)
    f.read(4)  # trailing record marker
    return payload


def read_slf(path: str | Path, *, frame: int = -1) -> dict:
    """Read a SERAFIN/SELAFIN file (our writer's output or a TELEMAC result).

    Returns a dict with ``x``, ``y`` (NPOIN floats), ``ikle`` (NELEM×NDP, 0-based),
    ``ipobo``, ``var_names`` (list[str]), ``nplan``, the selected frame ``time`` and
    its per-variable node arrays in ``values``. *frame* indexes the time steps
    (default ``-1`` = last; a steady run's final state). Single- and double-precision
    SERAFIN are both handled.

    **3D results** (``nplan > 1``, e.g. a TELEMAC-3D ``r3d.slf``) are unrolled rather
    than left flat: SERAFIN stores a 3D field plane by plane over the same 2D mesh, so
    ``x``/``y`` are cut back to the ``npoin2`` plan nodes and every variable comes back
    shaped ``(nplan, npoin2)``. A 2D file is unchanged - ``nplan == 1`` and values stay
    ``(NPOIN,)`` - so nothing that reads a 2D result has to know this exists.

    ``ikle`` is left exactly as written (prisms, 6 nodes, indexing the flat 3D node
    numbering). Callers that need plan connectivity should use a 2D file; the 3D path
    exists to read *fields*, and hydromate samples those on a KD-tree over ``x``/``y``.
    """
    path = Path(path)
    with open(path, "rb") as f:
        title = _read_record(f)
        fdtype = np.dtype(">f8") if title[72:80].strip() == b"SERAFIND" else _F4
        nbv = np.frombuffer(_read_record(f), dtype=_I4)
        nbv1, nbv2 = int(nbv[0]), int(nbv[1])
        var_names = [
            _read_record(f)[:16].decode("ascii", "replace").strip()
            for _ in range(nbv1 + nbv2)
        ]
        iparam = np.frombuffer(_read_record(f), dtype=_I4)
        if iparam.size >= 10 and iparam[9] == 1:
            _read_record(f)  # DATE record
        dims = np.frombuffer(_read_record(f), dtype=_I4)
        nelem, npoin, ndp = int(dims[0]), int(dims[1]), int(dims[2])
        # NPLAN lives in IPARAM(7) - index 6 - and that is where TELEMAC-3D actually
        # writes it: a real r3d.slf carries iparam[6] = 5 while the dims record's
        # fourth slot stays 1, so reading dims[3] (the intuitive place, and what the
        # format description suggests) silently reports every 3D result as 2D.
        nplan = int(iparam[6]) if iparam.size > 6 and iparam[6] > 1 else 1
        if nplan == 1 and dims.size > 3 and dims[3] > 1:
            nplan = int(dims[3])
        if nplan > 1 and npoin % nplan:
            log.warning("%s: NPOIN %d is not divisible by NPLAN %d; reading it as 2D",
                        path.name, npoin, nplan)
            nplan = 1
        ikle = np.frombuffer(_read_record(f), dtype=_I4).reshape(nelem, ndp) - 1
        ipobo = np.frombuffer(_read_record(f), dtype=_I4).copy()
        x = np.frombuffer(_read_record(f), dtype=fdtype).astype(float)
        y = np.frombuffer(_read_record(f), dtype=fdtype).astype(float)

        times: list[float] = []
        frames: list[dict[str, np.ndarray]] = []
        while True:
            t_rec = _read_record(f)
            if t_rec is None:
                break
            times.append(float(np.frombuffer(t_rec, dtype=fdtype)[0]))
            frames.append({
                name: np.frombuffer(_read_record(f), dtype=fdtype).astype(float)
                for name in var_names
            })
    if not frames:
        raise ValueError(f"{path.name}: no time frames in SELAFIN file")
    values = frames[frame]
    if nplan > 1:
        npoin2 = x.size // nplan
        x, y = x[:npoin2], y[:npoin2]
        ipobo = ipobo[:npoin2]
        values = {name: v.reshape(nplan, npoin2) for name, v in values.items()}
    return {
        "x": x, "y": y, "ikle": ikle, "ipobo": ipobo, "var_names": var_names,
        "nplan": nplan, "time": times[frame], "values": values,
        "n_times": len(frames),
    }


def _write_selafin(
    path: Path,
    x: np.ndarray,
    y: np.ndarray,
    ikle: np.ndarray,
    ipobo: np.ndarray,
    variables: list[tuple[str, str, np.ndarray]],
    *,
    title: str,
    date: Sequence[int],
    double_precision: bool,
) -> Path:
    """Write a single-frame (t=0) 2D triangular-mesh SELAFIN.

    *variables* is an ordered list of ``(name, unit, values)`` written as the
    file's node variables; their ``values`` must each be length NPOIN.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ikle = np.asarray(ikle, dtype=np.int64)
    ipobo = np.asarray(ipobo, dtype=np.int64)

    npoin = x.size
    nelem = ikle.shape[0]
    ndp = ikle.shape[1]
    if ndp != 3:
        raise ValueError(f"only triangular meshes supported (NDP=3), got {ndp}")
    if y.size != npoin or ipobo.size != npoin:
        raise ValueError("x, y and ipobo must all have length NPOIN")
    for name, _, values in variables:
        if np.asarray(values).size != npoin:
            raise ValueError(f"{name} length {np.asarray(values).size} != NPOIN={npoin}")
    nbv1 = len(variables)

    # IPARAM: index 9 == 1 -> a DATE record follows; index 6 == 0 -> 2D.
    iparam = np.zeros(10, dtype=_I4)
    iparam[0] = 1
    iparam[9] = 1

    fdtype = _F8 if double_precision else _F4
    tag = b"SERAFIND" if double_precision else b"SERAFIN "

    with open(path, "wb") as f:
        # 1) title (72) + precision tag (8) = 80-byte record
        f.write(_record(f"{title:<72}".encode("ascii") + tag))
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
        f.write(_record(x.astype(fdtype).tobytes()))
        f.write(_record(y.astype(fdtype).tobytes()))
        # 11) one time frame: t=0 then each variable's NPOIN values
        f.write(_record(np.array([0.0], dtype=fdtype).tobytes()))
        for _, _, values in variables:
            f.write(_record(np.asarray(values, dtype=float).astype(fdtype).tobytes()))
    return path


def write_geometry(
    path: str | Path,
    x: np.ndarray,
    y: np.ndarray,
    ikle: np.ndarray,
    ipobo: np.ndarray,
    bottom: np.ndarray,
    *,
    friction_id: np.ndarray | None = None,
    roughness: np.ndarray | None = None,
    title: str = "hydromate geometry",
    date: Sequence[int] = (2026, 1, 1, 0, 0, 0),
    double_precision: bool = True,
) -> Path:
    """Write a 2D triangular-mesh geometry SELAFIN.

    Always writes the BOTTOM variable. If *friction_id* is given it is written as
    ``FRIC_ID`` (per node), which TELEMAC reads to map nodes to friction zones in
    the FRICTION DATA FILE (no separate ZONES FILE needed). If *roughness* is
    given it is written as ``BOTTOM FRICTION`` (per node, m) - the per-node
    roughness value (e.g. a Nikuradse k_s) that HydroBayesCal later adjusts.

    *double_precision* (default ``True``, the ``SERAFIND`` format) writes node
    coordinates and variables as 8-byte reals. This is **required** for fine
    meshes on real-world projected coordinates: a single-precision (R4) float has
    a ~24-bit mantissa, so at UTM northings of ~5.3e6 m the representable spacing
    (ULP) is ~0.5 m. Storing a sub-metre mesh single precision snaps neighbouring
    nodes onto identical coordinates, collapsing thin channel triangles into
    coincident / collinear / inverted cells that make TELEMAC abort with
    ``GEOELT: ... NEGATIVE DETERMINANT``. Only set ``False`` for coarse meshes
    where the coordinate granularity is comfortably below the cell size.

    Parameters
    ----------
    x, y : (NPOIN,) float arrays of node coordinates.
    ikle : (NELEM, 3) int array of 1-based node indices per triangle.
    ipobo : (NPOIN,) int array - boundary numbering (0 interior, k>0 on contour).
    bottom : (NPOIN,) float array of bed elevations (m).
    friction_id : optional (NPOIN,) int array of friction-zone ids per node.
    roughness : optional (NPOIN,) float array of per-node roughness values.
    """
    bottom = np.asarray(bottom, dtype=float)
    variables = [("BOTTOM", "M", bottom)]
    if friction_id is not None:
        variables.append(("FRIC_ID", "", np.asarray(friction_id, dtype=float)))
    if roughness is not None:
        variables.append(("BOTTOM FRICTION", "M", np.asarray(roughness, dtype=float)))
    return _write_selafin(Path(path), x, y, ikle, ipobo, variables,
                          title=title, date=date, double_precision=double_precision)


def write_initial_state(
    path: str | Path,
    x: np.ndarray,
    y: np.ndarray,
    ikle: np.ndarray,
    ipobo: np.ndarray,
    depth: np.ndarray,
    *,
    velocity_u: np.ndarray | None = None,
    velocity_v: np.ndarray | None = None,
    title: str = "hydromate initial conditions",
    date: Sequence[int] = (2026, 1, 1, 0, 0, 0),
    double_precision: bool = True,
) -> Path:
    """Write a hotstart SELAFIN holding VELOCITY U, VELOCITY V and WATER DEPTH.

    TELEMAC reads this as the ``PREVIOUS COMPUTATION FILE`` (which alone triggers
    the continuation since release 9.0) to initialise the flow field, matching
    variables by name on the *same* mesh as the geometry. It pre-wets the domain
    (e.g. the channel) to a given *depth* so the wetting front does not have to be
    advanced from a dry bed - a large time saving for steady runs.

    Parameters
    ----------
    x, y, ikle, ipobo : the geometry of the mesh this state initialises (must
        match the GEOMETRY FILE: same NPOIN/NELEM, node order and connectivity).
    depth : (NPOIN,) float array of initial water depths (m).
    velocity_u, velocity_v : optional (NPOIN,) initial velocity components (m/s);
        default to zero (a still pre-wetted pond, the usual hotstart).
    """
    depth = np.asarray(depth, dtype=float)
    npoin = depth.size
    u = np.zeros(npoin) if velocity_u is None else np.asarray(velocity_u, dtype=float)
    v = np.zeros(npoin) if velocity_v is None else np.asarray(velocity_v, dtype=float)
    variables = [
        ("VELOCITY U", "M/S", u),
        ("VELOCITY V", "M/S", v),
        ("WATER DEPTH", "M", depth),
    ]
    return _write_selafin(Path(path), x, y, ikle, ipobo, variables,
                          title=title, date=date, double_precision=double_precision)
