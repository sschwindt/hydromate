"""A self-contained OpenFOAM ``constant/polyMesh`` writer.

This is the OpenFOAM counterpart of :mod:`hydromate.core.selafin`: hydromate writes the
mesh itself rather than driving a mesher, so the mesh is exactly what the case
config asks for and nothing is left to a black box.

Why write the mesh directly instead of using snappyHexMesh
----------------------------------------------------------
snappyHexMesh castellates a background block against an STL and then *snaps* the
cut cells onto it. On a river bed - a single-valued, gently varying surface with no
thin features - that machinery buys nothing and costs a great deal: the snapped
layer is where the mesh degenerates (slivers, failed layer addition, negative
volumes), and every fix is a search through ``snappyHexMeshDict`` for the knob that
happens to help. A river bed does not need to be *snapped to*; it can be **followed**,
because it is a height field z_b(x, y). Extruding a plan grid between the bed and a
lid gives a mesh that is:

* **all-hexahedral** - every cell is a hex, no polyhedra, no cut cells;
* **structured** - a plain (i, j, k) grid (with the columns outside the domain
  simply absent), so the addressing is deterministic and the cell count is known
  before anything is built;
* **conformal by construction** - the vertical levels are computed at the *plan
  vertices*, so neighbouring columns share their corner points exactly and no
  matching or merging step is needed;
* **boundary-fitted at the bed** - the bottom face of the lowest cell *is* the DEM
  surface, so the wall functions see the real bed, not a staircase.

The price is non-orthogonality wherever the bed is steep, which is measured
(:mod:`hydromate.solvers.openfoam.quality`) rather than assumed away.

Format notes
------------
``polyMesh`` requires, and this writer guarantees:

* internal faces first, ordered **upper-triangular**: ascending ``owner``, and
  within one owner ascending ``neighbour``; ``owner < neighbour`` on every one;
* boundary faces after them, contiguous per patch, in the ``boundary`` file order;
* every face's normal (right-hand rule over its point list) pointing from its owner
  cell to its neighbour - out of the domain for a boundary face.

Files are written in **ASCII**. The mesh is written once and read once; the run's
own output is binary (``writeFormat binary`` in ``controlDict``), and
``decomposePar`` writes the per-processor meshes in that format too, so ASCII here
costs one slow read and buys a mesh that can be inspected and diffed.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("hydromate")

FOAM_VERSION = "9"

_HEADER = """/*--------------------------------*- C++ -*----------------------------------*\\
  =========                 |
  \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\\\    /   O peration     | Website:  https://openfoam.org
    \\\\  /    A nd           | Version:  {version}
     \\\\/     M anipulation  | Written by hydromate
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    format      {fmt};
    class       {cls};
{location}    object      {obj};
}}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

"""

_FOOTER = "\n\n// ************************************************************************* //\n"


def foam_header(cls: str, obj: str, *, location: str | None = None,
                fmt: str = "ascii") -> str:
    """Render the ``FoamFile`` banner every OpenFOAM file starts with."""
    loc = f'    location    "{location}";\n' if location else ""
    return _HEADER.format(version=FOAM_VERSION, fmt=fmt, cls=cls, obj=obj, location=loc)


def foam_footer() -> str:
    return _FOOTER


# --------------------------------------------------------------------------- #
# mesh containers
# --------------------------------------------------------------------------- #


@dataclass
class Patch:
    """One boundary patch: a contiguous run of faces in the global face list."""

    name: str
    type: str = "patch"          # patch | wall | empty | symmetryPlane | cyclic
    n_faces: int = 0
    start_face: int = 0
    in_groups: tuple[str, ...] = ()

    def render(self) -> str:
        groups = ""
        if self.in_groups:
            groups = f"        inGroups        1({' '.join(self.in_groups)});\n"
        return (f"    {self.name}\n"
                f"    {{\n"
                f"        type            {self.type};\n"
                f"{groups}"
                f"        nFaces          {self.n_faces};\n"
                f"        startFace       {self.start_face};\n"
                f"    }}\n")


@dataclass
class PolyMesh:
    """An assembled OpenFOAM mesh, ready to write.

    ``faces`` is an ``(NF, 4)`` integer array because every face of an all-hex mesh
    is a quadrilateral; the writer emits the general ``4(a b c d)`` face syntax, so
    OpenFOAM neither knows nor cares that they are all quads.
    """

    points: np.ndarray                     # (NP, 3) float
    faces: np.ndarray                      # (NF, 4) int, internal faces first
    owner: np.ndarray                      # (NF,) int
    neighbour: np.ndarray                  # (NI,) int, internal faces only
    patches: list[Patch] = field(default_factory=list)
    n_cells: int = 0

    @property
    def n_points(self) -> int:
        return int(self.points.shape[0])

    @property
    def n_faces(self) -> int:
        return int(self.faces.shape[0])

    @property
    def n_internal_faces(self) -> int:
        return int(self.neighbour.size)

    def patch(self, name: str) -> Patch:
        for p in self.patches:
            if p.name == name:
                return p
        raise KeyError(f"no patch named {name!r}; have {[p.name for p in self.patches]}")

    def patch_face_ids(self, name: str) -> np.ndarray:
        """Global face indices belonging to *name* (the order the BC list must use)."""
        p = self.patch(name)
        return np.arange(p.start_face, p.start_face + p.n_faces)

    def face_centres(self, ids: np.ndarray | None = None) -> np.ndarray:
        f = self.faces if ids is None else self.faces[ids]
        return self.points[f].mean(axis=1)

    def face_normals(self, ids: np.ndarray | None = None) -> np.ndarray:
        """Area-weighted face normals (Newell over the quad)."""
        f = self.faces if ids is None else self.faces[ids]
        p = self.points[f]
        return 0.5 * np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1])

    def face_areas(self, ids: np.ndarray | None = None) -> np.ndarray:
        return np.linalg.norm(self.face_normals(ids), axis=1)

    # ---------------------------------------------------------------- writing
    def write(self, case_dir: str | Path) -> Path:
        return write_polymesh(self, case_dir)


# --------------------------------------------------------------------------- #
# assembly: ordering + orientation
# --------------------------------------------------------------------------- #


def _quad_normals(points: np.ndarray, quads: np.ndarray) -> np.ndarray:
    p = points[quads]
    return 0.5 * np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1])


def assemble(points: np.ndarray,
             internal: tuple[np.ndarray, np.ndarray, np.ndarray],
             boundary: list[tuple[str, str, np.ndarray, np.ndarray]],
             cell_centres: np.ndarray) -> PolyMesh:
    """Order and orient raw face lists into a valid :class:`PolyMesh`.

    *internal* is ``(owner, neighbour, quads)`` for the internal faces in any order;
    *boundary* is a list of ``(patch_name, patch_type, owner, quads)``. *cell_centres*
    supplies the owner-to-neighbour direction used to orient every face - the
    structured builder knows them exactly, so orientation needs no topological
    reasoning.

    Faces whose owner exceeds their neighbour are swapped (both the pair and the
    point order) rather than rejected, so a caller may emit either sense.
    """
    owner, neigh, quads = (np.asarray(a) for a in internal)
    owner = owner.astype(np.int64, copy=True)
    neigh = neigh.astype(np.int64, copy=True)
    quads = quads.astype(np.int64, copy=True)

    # owner < neighbour on every internal face
    flip = owner > neigh
    if flip.any():
        owner[flip], neigh[flip] = neigh[flip], owner[flip].copy()
    if (owner == neigh).any():
        raise ValueError("internal face with owner == neighbour (degenerate cell pair)")

    # upper-triangular order: ascending owner, then ascending neighbour
    order = np.lexsort((neigh, owner))
    owner, neigh, quads = owner[order], neigh[order], quads[order]

    # orient: normal points owner -> neighbour
    normals = _quad_normals(points, quads)
    wrong = np.einsum("ij,ij->i", normals, cell_centres[neigh] - cell_centres[owner]) < 0
    if wrong.any():
        quads[wrong] = quads[wrong][:, ::-1]

    all_quads = [quads]
    all_owner = [owner]
    patches: list[Patch] = []
    start = quads.shape[0]
    for name, ptype, bowner, bquads in boundary:
        bowner = np.asarray(bowner, dtype=np.int64)
        bquads = np.asarray(bquads, dtype=np.int64).copy()
        if bowner.size:
            normals = _quad_normals(points, bquads)
            centres = points[bquads].mean(axis=1)
            outward = np.einsum("ij,ij->i", normals, centres - cell_centres[bowner]) > 0
            if (~outward).any():
                bquads[~outward] = bquads[~outward][:, ::-1]
        patches.append(Patch(name=name, type=ptype, n_faces=int(bowner.size),
                             start_face=int(start),
                             in_groups=("wall",) if ptype == "wall" else ()))
        all_quads.append(bquads)
        all_owner.append(bowner)
        start += int(bowner.size)

    return PolyMesh(
        points=np.asarray(points, dtype=float),
        faces=np.concatenate(all_quads, axis=0),
        owner=np.concatenate(all_owner),
        neighbour=neigh,
        patches=patches,
        n_cells=int(cell_centres.shape[0]),
    )


# --------------------------------------------------------------------------- #
# file writers
# --------------------------------------------------------------------------- #


def _write_scalar_list(fh, values: np.ndarray, cls: str, obj: str) -> None:
    fh.write(foam_header(cls, obj, location="constant/polyMesh"))
    fh.write(f"{values.size}\n(\n")
    buf = io.StringIO()
    np.savetxt(buf, values, fmt="%d")
    fh.write(buf.getvalue())
    fh.write(")\n")
    fh.write(foam_footer())


def _write_points(fh, points: np.ndarray) -> None:
    fh.write(foam_header("vectorField", "points", location="constant/polyMesh"))
    fh.write(f"{points.shape[0]}\n(\n")
    buf = io.StringIO()
    np.savetxt(buf, points, fmt="(%.10g %.10g %.10g)")
    fh.write(buf.getvalue())
    fh.write(")\n")
    fh.write(foam_footer())


def _write_faces(fh, faces: np.ndarray) -> None:
    fh.write(foam_header("faceList", "faces", location="constant/polyMesh"))
    fh.write(f"{faces.shape[0]}\n(\n")
    n = faces.shape[1]
    fmt = f"{n}(" + " ".join(["%d"] * n) + ")"
    # chunked so a multi-million-face mesh does not build one giant string
    for start in range(0, faces.shape[0], 500_000):
        buf = io.StringIO()
        np.savetxt(buf, faces[start:start + 500_000], fmt=fmt)
        fh.write(buf.getvalue())
    fh.write(")\n")
    fh.write(foam_footer())


def _write_boundary(fh, patches: list[Patch]) -> None:
    fh.write(foam_header("polyBoundaryMesh", "boundary", location="constant/polyMesh"))
    fh.write(f"{len(patches)}\n(\n")
    for p in patches:
        fh.write(p.render())
    fh.write(")\n")
    fh.write(foam_footer())


def write_polymesh(mesh: PolyMesh, case_dir: str | Path) -> Path:
    """Write ``<case_dir>/constant/polyMesh/{points,faces,owner,neighbour,boundary}``."""
    out = Path(case_dir) / "constant" / "polyMesh"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "points", "w") as fh:
        _write_points(fh, mesh.points)
    with open(out / "faces", "w") as fh:
        _write_faces(fh, mesh.faces)
    with open(out / "owner", "w") as fh:
        _write_scalar_list(fh, mesh.owner, "labelList", "owner")
    with open(out / "neighbour", "w") as fh:
        _write_scalar_list(fh, mesh.neighbour, "labelList", "neighbour")
    with open(out / "boundary", "w") as fh:
        _write_boundary(fh, mesh.patches)
    log.info("polyMesh written to %s (%d cells, %d faces, %d points)",
             out, mesh.n_cells, mesh.n_faces, mesh.n_points)
    return out


# --------------------------------------------------------------------------- #
# validation (what OpenFOAM itself would refuse to read)
# --------------------------------------------------------------------------- #


def read_patch_names(case_dir: str | Path) -> list[str]:
    """Patch names from a written ``constant/polyMesh/boundary``, in file order.

    Lets the dictionaries be regenerated against an existing mesh without rebuilding
    it - which is what keeps a run honouring the config it was *launched* with rather
    than the one it was built with.
    """
    path = Path(case_dir) / "constant" / "polyMesh" / "boundary"
    if not path.is_file():
        return []
    text = path.read_text()
    # drop the FoamFile header dict, whose closing brace would otherwise look like
    # the end of a patch entry
    header = text.find("FoamFile")
    if header >= 0:
        end = text.find("}", header)
        text = text[end + 1:] if end >= 0 else text

    names: list[str] = []
    depth = 0
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        if not line:
            continue
        if line == "{":
            if depth == 0 and pending:      # the name sits just above its dict
                names.append(pending)
            depth += 1
            pending = None
            continue
        if line == "}":
            depth = max(depth - 1, 0)
            pending = None
            continue
        if depth == 0 and line not in ("(", ")") and not line.endswith(";") \
                and not line.isdigit():
            pending = line
    return names


def validate(mesh: PolyMesh) -> list[str]:
    """Return the structural problems that would make OpenFOAM reject the mesh.

    Cheap, exact checks on the addressing only - geometric quality (orthogonality,
    skewness, volume) is :mod:`hydromate.solvers.openfoam.quality`'s job.
    """
    problems: list[str] = []
    ni = mesh.n_internal_faces
    if mesh.owner.size != mesh.n_faces:
        problems.append(f"owner has {mesh.owner.size} entries for {mesh.n_faces} faces")
    if ni > mesh.n_faces:
        problems.append("more neighbours than faces")
    if ni and (mesh.owner[:ni] >= mesh.neighbour).any():
        problems.append("internal face with owner >= neighbour (not upper-triangular)")
    if ni > 1:
        key = mesh.owner[:ni].astype(np.int64) * (mesh.n_cells + 1) + mesh.neighbour
        if (np.diff(key) < 0).any():
            problems.append("internal faces are not in upper-triangular order")
    if mesh.faces.max(initial=-1) >= mesh.n_points or mesh.faces.min(initial=0) < 0:
        problems.append("face references a point index outside the point list")
    if mesh.owner.max(initial=-1) >= mesh.n_cells:
        problems.append("owner references a cell index outside the cell range")
    # every cell must be closed: its face area vectors must sum to ~zero
    signed = mesh.face_normals()
    flux = np.zeros((mesh.n_cells, 3))
    np.add.at(flux, mesh.owner, signed)
    np.add.at(flux, mesh.neighbour, -signed[:ni])
    scale = np.maximum(mesh.face_areas().max(), 1e-30)
    open_cells = int((np.linalg.norm(flux, axis=1) > 1e-6 * scale).sum())
    if open_cells:
        problems.append(f"{open_cells} cells are not closed (face area vectors do not sum to 0)")
    used = np.zeros(mesh.n_points, dtype=bool)
    used[mesh.faces.ravel()] = True
    if not used.all():
        problems.append(f"{int((~used).sum())} unused points in the point list")
    return problems
