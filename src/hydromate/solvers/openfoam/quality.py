"""Mesh quality and physical validity for the OpenFOAM case, before OpenFOAM runs.

Two separate questions, both answered here because both must be answered *before*
committing solver hours:

**Is the mesh numerically sound?** Non-orthogonality, skewness, aspect ratio, cell
volume - the same measures ``checkMesh`` reports, computed in numpy so the answer
arrives while the mesh is still in memory and a bad ``cell_size`` / ``n_layers``
combination can be corrected in seconds rather than after a decomposition and a
failed run. ``checkMesh`` is still run by :mod:`hydromate.solvers.openfoam.runtime` and
remains the authority; this is the fast pre-check, exactly as
:mod:`hydromate.mesh_quality` is for the TELEMAC mesh.

**Is the wall treatment admissible?** This is the check that matters on a gravel-bed
river and the one that has no analogue in an aerodynamics workflow. OpenFOAM's
``nutkRoughWallFunction`` manipulates the log-law ``E`` parameter for a sand-grain
roughness ``Ks``, and that manipulation is only meaningful while the first cell
centre stands *clear* of the roughness elements - ``y_1 > Ks``, i.e. a bed layer at
least ``2 Ks`` thick. On the isar reach ``ks`` runs 0.085-0.22 m in 0.3-0.9 m of
water, so a "well resolved" bed layer of a few centimetres would put the first cell
centre *inside* the gravel and the wall function would return a confident,
meaningless answer. Refining towards the bed is therefore **not** available here,
and the report says so per bed face instead of letting the run imply otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from hydromate.mesh_validity import (
    KS_PLUS_ROUGH, KS_PLUS_SMOOTH, NU_WATER, roughness_reynolds,
    shear_velocity_loglaw,
)

log = logging.getLogger("hydromate")

# checkMesh's own thresholds, so the two reports agree on what "bad" means
MAX_NON_ORTHO = 70.0        # deg; above this checkMesh calls the mesh severely non-orthogonal
WARN_NON_ORTHO = 65.0       # deg; above this it warns
MAX_SKEWNESS = 4.0          # internal faces
MAX_ASPECT_RATIO = 1000.0


@dataclass
class MeshReport:
    """Everything measurable about the mesh without launching OpenFOAM."""

    n_cells: int
    n_faces: int
    n_points: int
    n_internal_faces: int
    patches: dict[str, int] = field(default_factory=dict)

    min_volume: float = 0.0
    max_volume: float = 0.0
    total_volume: float = 0.0
    max_non_ortho: float = 0.0
    mean_non_ortho: float = 0.0
    n_non_ortho_severe: int = 0
    max_skewness: float = 0.0
    n_skew: int = 0
    n_bad_pyramid: int = 0
    max_aspect_ratio: float = 0.0
    n_bad_volume: int = 0

    # wall-function admissibility on the bed
    bed_layer_min: float = 0.0
    bed_layer_median: float = 0.0
    ks_min: float | None = None
    ks_max: float | None = None
    n_bed_faces: int = 0
    n_wall_function_invalid: int = 0
    ks_plus: float | None = None
    regime: str = "unknown"

    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when the mesh is structurally sound enough to run on.

        Deliberately **not** the same as passing every ``checkMesh`` check. A mesh
        derived from a real DEM will occasionally carry a handful of marginal faces
        where the survey has a seam or a step - the isar reach produces exactly one
        skew face over the limit in 4.9 million, and no amount of lid smoothing moves
        it because it comes from the bathymetry, not from the meshing. Refusing to
        run on that would be refusing to run on the terrain. What genuinely stops a
        run is a cell with no volume or an unclosed cell, and those are what this
        reports.
        """
        return (self.min_volume > 0.0 and self.n_bad_volume == 0
                and self.n_bad_pyramid == 0)

    @property
    def ok(self) -> bool:
        """True when the mesh would pass every ``checkMesh`` check."""
        return (self.usable
                and self.max_non_ortho < MAX_NON_ORTHO
                and self.max_skewness < MAX_SKEWNESS
                and self.max_aspect_ratio < MAX_ASPECT_RATIO)

    @property
    def marginal_faces(self) -> int:
        """Faces that exceed a ``checkMesh`` limit without breaking the mesh."""
        return self.n_non_ortho_severe + self.n_skew

    def lines(self) -> list[str]:
        out = [
            f"mesh: {self.n_cells:,} cells, {self.n_faces:,} faces, "
            f"{self.n_points:,} points ({self.n_internal_faces:,} internal)",
            "  patches      : " + ", ".join(f"{k} {v:,}" for k, v in self.patches.items()),
            f"  cell volume  : {self.min_volume:.3e} .. {self.max_volume:.3e} m3 "
            f"(total {self.total_volume:,.0f} m3)",
            f"  non-ortho    : mean {self.mean_non_ortho:.1f} deg, "
            f"max {self.max_non_ortho:.1f} deg "
            f"({self.n_non_ortho_severe:,} faces over {MAX_NON_ORTHO:g} deg)",
            f"  skewness     : max {self.max_skewness:.2f} "
            f"({self.n_skew:,} faces over {MAX_SKEWNESS:g})",
            f"  aspect ratio : max {self.max_aspect_ratio:.1f}",
            f"  face pyramids: {self.n_bad_pyramid:,} incorrectly oriented",
        ]
        if self.n_bed_faces:
            out.append(f"  bed layer    : min {self.bed_layer_min:.3f} m, "
                       f"median {self.bed_layer_median:.3f} m "
                       f"(over {self.n_bed_faces:,} wetted bed faces)")
        if self.ks_max is not None:
            out.append(f"  bed ks       : {self.ks_min:.3f} .. {self.ks_max:.3f} m"
                       + (f"  (ks+ ~ {self.ks_plus:.0f}, {self.regime})"
                          if self.ks_plus is not None else ""))
            share = 100.0 * self.n_wall_function_invalid / max(self.n_bed_faces, 1)
            out.append(f"  wall function: {self.n_wall_function_invalid:,} of "
                       f"{self.n_bed_faces:,} wetted bed faces ({share:.1f}%) have the "
                       f"first cell centre inside the roughness (y1 < ks)")
        for w in self.warnings:
            out.append(f"  ! {w}")
        return out


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #


def face_geometry(mesh) -> tuple[np.ndarray, np.ndarray]:
    """``(face centres, face area vectors)`` by OpenFOAM's triangle decomposition.

    A quad on a terrain-following mesh is generally **not planar** - its four corners
    sit on four columns of different height - so its centroid is not the average of
    its corners. OpenFOAM fans the face about that average and area-weights the
    resulting triangles; anything cruder shifts the centre by a few percent of the
    cell, which is enough to move the reported non-orthogonality by several degrees.
    """
    p = mesh.points[mesh.faces]                       # (NF, 4, 3)
    centre_est = p.mean(axis=1)
    nxt = np.roll(p, -1, axis=1)
    tri_centroid = p + nxt + centre_est[:, None, :]    # 3x the triangle centroid
    tri_normal = np.cross(nxt - p, centre_est[:, None, :] - p)
    tri_area = np.linalg.norm(tri_normal, axis=2)
    total = tri_area.sum(axis=1)
    safe = np.where(total > 0, total, 1.0)
    centres = np.einsum("ij,ijk->ik", tri_area, tri_centroid) / (3.0 * safe[:, None])
    centres = np.where(total[:, None] > 0, centres, centre_est)
    return centres, 0.5 * tri_normal.sum(axis=1)


def cell_geometry(mesh, face_centres: np.ndarray,
                  face_normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(cell centres, cell volumes)`` by OpenFOAM's pyramid decomposition.

    Each face forms a pyramid with an estimated cell centre; the true centre is the
    volume-weighted mean of the pyramid centroids. Reproducing this exactly is what
    makes the non-orthogonality reported here the same number ``checkMesh`` will
    report, rather than an optimistic approximation of it.
    """
    ni = mesh.n_internal_faces
    n_cells = mesh.n_cells
    # first estimate: mean of the cell's own face centres
    acc = np.zeros((n_cells, 3))
    count = np.zeros(n_cells)
    np.add.at(acc, mesh.owner, face_centres)
    np.add.at(count, mesh.owner, 1.0)
    np.add.at(acc, mesh.neighbour, face_centres[:ni])
    np.add.at(count, mesh.neighbour, 1.0)
    estimate = acc / np.maximum(count, 1.0)[:, None]

    centres = np.zeros((n_cells, 3))
    volumes = np.zeros(n_cells)

    def accumulate(cells, sign, sl):
        pyr3 = sign * np.einsum(
            "ij,ij->i", face_normals[sl], face_centres[sl] - estimate[cells])
        centroid = 0.75 * face_centres[sl] + 0.25 * estimate[cells]
        np.add.at(centres, cells, pyr3[:, None] * centroid)
        np.add.at(volumes, cells, pyr3)

    accumulate(mesh.owner, 1.0, slice(None))
    accumulate(mesh.neighbour, -1.0, slice(0, ni))
    safe = np.where(np.abs(volumes) > 1e-300, volumes, 1.0)
    return centres / safe[:, None], volumes / 3.0


def bad_face_pyramids(mesh, face_centres: np.ndarray,
                      face_normals: np.ndarray,
                      cell_centres: np.ndarray) -> np.ndarray:
    """Faces whose pyramid to their cell centre has non-positive volume.

    checkMesh calls these "incorrectly oriented" and treats them as fatal, and rightly:
    the cell is folded even though its total volume can still come out positive - which
    is exactly how a first version of this report passed a mesh checkMesh rejected.
    A rigid-lid mesh produces them at the waterline, where the water column thins to
    nothing and the cells collapse.
    """
    ni = mesh.n_internal_faces
    owner_pyr = np.einsum("ij,ij->i", face_normals,
                          face_centres - cell_centres[mesh.owner])
    bad = owner_pyr <= 0
    neigh_pyr = np.einsum("ij,ij->i", -face_normals[:ni],
                          face_centres[:ni] - cell_centres[mesh.neighbour])
    bad[:ni] |= neigh_pyr <= 0
    return bad


def non_orthogonality(mesh, face_normals: np.ndarray,
                      cell_centres: np.ndarray) -> tuple[np.ndarray, float]:
    """``(per-face angle [deg], mean [deg])`` for the internal faces.

    The mean is ``checkMesh``'s: it averages the *cosines* and takes the arccosine
    of that, not the mean of the angles - the two differ by a couple of degrees on a
    mesh like this, and quoting the second while calling it the first would make two
    reports of the same mesh disagree for no reason.
    """
    ni = mesh.n_internal_faces
    d = cell_centres[mesh.neighbour] - cell_centres[mesh.owner[:ni]]
    sf = face_normals[:ni]
    cos = np.einsum("ij,ij->i", d, sf) / (
        np.linalg.norm(d, axis=1) * np.linalg.norm(sf, axis=1) + 1e-300)
    cos = np.clip(cos, -1.0, 1.0)
    mean = float(np.degrees(np.arccos(cos.mean()))) if cos.size else 0.0
    return np.degrees(np.arccos(cos)), mean


def skewness(mesh, face_centres: np.ndarray, face_normals: np.ndarray,
             cell_centres: np.ndarray) -> np.ndarray:
    """Face skewness, matching OpenFOAM's ``primitiveMeshTools::faceSkewness``.

    The skewness *vector* is the offset of the owner-neighbour line's crossing point
    from the face centre; what matters is the normalisation. OpenFOAM divides it by
    the face's own half-width in the skew direction (floored at ``0.2 |d|`` for an
    internal face, ``0.4 |d|`` for a boundary one), **not** by the cell-to-cell
    distance. Dividing by ``|d|`` instead - the intuitive choice - understates the
    result by roughly the face aspect ratio, which on this mesh is a factor of ~7:
    the number would look reassuring while sitting near ``checkMesh``'s limit.

    Boundary faces are included, as ``checkMesh`` includes them, by mirroring the
    owner cell across the face.
    """
    ni = mesh.n_internal_faces
    own = cell_centres[mesh.owner]
    d = np.empty_like(own)
    d[:ni] = cell_centres[mesh.neighbour] - own[:ni]
    # boundary: mirror the owner across the face along the face normal
    nb = face_normals[ni:] / (np.linalg.norm(face_normals[ni:], axis=1,
                                             keepdims=True) + 1e-300)
    cpf_b = face_centres[ni:] - own[ni:]
    d[ni:] = nb * np.einsum("ij,ij->i", nb, cpf_b)[:, None]

    cpf = face_centres - own
    denom = np.einsum("ij,ij->i", face_normals, d)
    denom = np.where(np.abs(denom) < 1e-300, 1e-300, denom)
    scale = np.einsum("ij,ij->i", face_normals, cpf) / denom
    sv = cpf - scale[:, None] * d
    sv_mag = np.linalg.norm(sv, axis=1)
    sv_hat = sv / (sv_mag[:, None] + 1e-300)

    floor = np.empty(mesh.n_faces)
    d_mag = np.linalg.norm(d, axis=1)
    floor[:ni] = 0.2 * d_mag[:ni]
    floor[ni:] = 0.4 * d_mag[ni:]
    # half-width of the face in the skew direction
    offsets = mesh.points[mesh.faces] - face_centres[:, None, :]
    reach = np.abs(np.einsum("ijk,ik->ij", offsets, sv_hat)).max(axis=1)
    fd = np.maximum(floor, reach) + 1e-300
    return sv_mag / fd


def aspect_ratios(mesh, face_normals: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    """Cell aspect ratio, matching OpenFOAM's ``primitiveMeshTools::cellClosedness``.

    ``max(component ratio, hydraulic ratio)`` where the component ratio is the
    largest over the smallest of the per-direction summed face-area magnitudes, and
    the hydraulic one is ``(1/6) sum|Sf| / V^(2/3)``. The component form dominates on
    a terrain-following mesh - a cell a metre wide in plan and a decimetre tall has
    ten times the horizontal face area of the vertical - and it is precisely the
    number that exposes how flat the layers are, so taking only the hydraulic form
    (which reads ~3 where OpenFOAM reads ~17) would hide it.
    """
    ni = mesh.n_internal_faces
    per_dir = np.zeros((volumes.shape[0], 3))
    magnitudes = np.abs(face_normals)
    np.add.at(per_dir, mesh.owner, magnitudes)
    np.add.at(per_dir, mesh.neighbour, magnitudes[:ni])
    component = per_dir.max(axis=1) / (per_dir.min(axis=1) + 1e-300)
    safe = np.where(volumes > 0, volumes, np.nan)
    hydraulic = per_dir.sum(axis=1) / (6.0 * safe ** (2.0 / 3.0))
    return np.maximum(component, hydraulic)


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def assess(of_mesh, *, state=None, roughness_constant: float = 0.5) -> MeshReport:
    """Measure *of_mesh* (an :class:`~hydromate.solvers.openfoam.mesh.OpenFoamMesh`).

    *state* is the optional 2D hotstart; with one, the roughness regime (``ks+``) is
    evaluated at the reach's own depth and velocity rather than guessed.
    """
    mesh = of_mesh.polymesh
    # computed the way OpenFOAM computes them, so the numbers below are the ones
    # checkMesh will print rather than an approximation of them
    fc, fn = face_geometry(mesh)
    cc, vol = cell_geometry(mesh, fc, fn)

    nonortho, nonortho_mean = non_orthogonality(mesh, fn, cc)
    bad_pyr = bad_face_pyramids(mesh, fc, fn, cc)
    skew = skewness(mesh, fc, fn, cc)
    ar = aspect_ratios(mesh, fn, vol)

    report = MeshReport(
        n_cells=mesh.n_cells, n_faces=mesh.n_faces, n_points=mesh.n_points,
        n_internal_faces=mesh.n_internal_faces,
        patches={p.name: p.n_faces for p in mesh.patches},
        min_volume=float(vol.min()), max_volume=float(vol.max()),
        total_volume=float(vol.sum()),
        max_non_ortho=float(nonortho.max(initial=0.0)),
        mean_non_ortho=nonortho_mean,
        n_non_ortho_severe=int((nonortho > MAX_NON_ORTHO).sum()),
        max_skewness=float(skew.max(initial=0.0)),
        n_skew=int((skew > MAX_SKEWNESS).sum()),
        n_bad_volume=int((vol <= 0).sum()),
        n_bad_pyramid=int(bad_pyr.sum()),
        max_aspect_ratio=float(np.nanmax(ar)) if ar.size else 0.0,
    )

    if report.n_bad_pyramid:
        report.warnings.append(
            f"{report.n_bad_pyramid} face(s) are incorrectly oriented (a pyramid to "
            "the cell centre has non-positive volume). checkMesh treats this as fatal. "
            "On a rigid-lid mesh it comes from the waterline, where the water column "
            "thins to nothing - raise openfoam.min_column_height so those columns are "
            "left out.")
    if report.min_volume <= 0:
        report.warnings.append(
            f"{int((vol <= 0).sum())} cells have zero or negative volume - the mesh "
            "is inverted somewhere. Coarsen openfoam.cell_size or raise "
            "openfoam.min_column_height.")
    if report.max_non_ortho >= MAX_NON_ORTHO:
        report.warnings.append(
            f"max non-orthogonality {report.max_non_ortho:.1f} deg exceeds "
            f"{MAX_NON_ORTHO:g} deg; raise nNonOrthogonalCorrectors, or coarsen the "
            "plan grid so the bed slope is spread over fewer, wider cells.")
    elif report.max_non_ortho >= WARN_NON_ORTHO:
        report.warnings.append(
            f"max non-orthogonality {report.max_non_ortho:.1f} deg is high (steep bed); "
            "the emitted fvSolution already carries nNonOrthogonalCorrectors 2.")
    if report.n_skew:
        share = report.n_skew / max(report.n_faces, 1)
        report.warnings.append(
            f"{report.n_skew:,} of {report.n_faces:,} faces ({100 * share:.4f}%) exceed "
            f"skewness {MAX_SKEWNESS:g} (max {report.max_skewness:.2f}); checkMesh will "
            "report this as a failed check. A handful usually traces to a step or a "
            "survey seam in the DEM rather than to the meshing - raise "
            "openfoam.lid_smoothing if they sit at the surface, otherwise this is the "
            "terrain and the run is unaffected.")

    _assess_wall_function(of_mesh, report, state=state,
                          roughness_constant=roughness_constant)
    for line in report.lines():
        log.info("%s", line)
    return report


def _assess_wall_function(of_mesh, report: MeshReport, *, state,
                          roughness_constant: float) -> None:
    """Is the rough wall function admissible on the bed layer as built?"""
    from hydromate.solvers.openfoam.mesh import column_corner_vertices

    corners = column_corner_vertices(of_mesh.grid)
    first = of_mesh.first_layer_height[corners].mean(axis=1)   # per column [m]
    report.n_bed_faces = int(first.size)
    report.bed_layer_min = float(first.min())
    report.bed_layer_median = float(np.median(first))

    ks = of_mesh.bed_ks
    if ks is None:
        return
    ks = np.asarray(ks, dtype=float)
    # Only the WETTED bed is judged. A dry gravel bar carries the floodplain /
    # vegetation ks (isar: 0.5 m) under no water at all; counting it would report a
    # wall-function failure that has no bearing on the flow, and would drown the
    # places where it does matter.
    wet = (of_mesh.column_depth > 0.0) if of_mesh.column_depth is not None \
        else np.ones(ks.shape, dtype=bool)
    if not wet.any():
        wet = np.ones(ks.shape, dtype=bool)
    report.n_bed_faces = int(wet.sum())
    report.bed_layer_min = float(first[wet].min())
    report.bed_layer_median = float(np.median(first[wet]))
    report.ks_min = float(np.nanmin(ks[wet]))
    report.ks_max = float(np.nanmax(ks[wet]))
    # the first cell CENTRE sits at half the layer height; the wall function needs
    # it clear of the roughness elements
    report.n_wall_function_invalid = int(((0.5 * first[wet]) < ks[wet]).sum())
    ks = ks[wet]

    if state is not None:
        depth = state.depth_scale()
        velocity = state.velocity_scale()
        u_star = shear_velocity_loglaw(depth, velocity, float(np.nanmedian(ks)))
        if np.isfinite(u_star):
            ks_plus = roughness_reynolds(u_star, float(np.nanmedian(ks)), NU_WATER)
            report.ks_plus = float(ks_plus)
            report.regime = ("fully rough" if ks_plus >= KS_PLUS_ROUGH else
                             "transitional" if ks_plus >= KS_PLUS_SMOOTH else
                             "hydraulically smooth")

    share = report.n_wall_function_invalid / max(report.n_bed_faces, 1)
    if share > 0.05:
        report.warnings.append(
            f"{100 * share:.0f}% of WETTED bed faces have the first cell centre inside "
            f"the roughness (y1 < ks, ks up to {report.ks_max:.3f} m). "
            "nutkRoughWallFunction will NOT fail there - it clamps y+ to e/E and "
            "carries on - so the run completes and the near-bed velocity is simply "
            "not physical. Either raise openfoam.bed_layer_height towards 2*ks (fewer, "
            "thicker near-bed layers) or accept that this reach's roughness is "
            "macro-scale and read the result as bulk flow, not as a bed boundary "
            "layer.")
    if report.regime == "hydraulically smooth":
        report.warnings.append(
            "ks+ places the bed in the hydraulically smooth regime; a rough wall "
            "function adds nothing there.")
