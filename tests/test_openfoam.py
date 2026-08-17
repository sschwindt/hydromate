"""The OpenFOAM free-surface extension (:mod:`axqua.solvers.openfoam`).

Pure-python: no OpenFOAM, no solver, no geodata. The mesh under test is a small
hand-built lattice, which is enough to pin down the things that would otherwise only
fail once ``checkMesh`` or ``interFoam`` had been launched on a million cells:

* the ``polyMesh`` **addressing contract** - internal faces first in upper-triangular
  order, ``owner < neighbour`` on every one, every face oriented owner-to-neighbour,
  every cell closed. Getting any of these wrong produces a mesh OpenFOAM refuses to
  read, and the failure message points at the file rather than at the bug;
* the **sigma distribution**, in particular that pinning the bed layer never eats the
  column and always lands exactly on 1 at the top - the mesh is conformal only
  because neighbouring columns share their vertex levels exactly;
* the **geometric measures**, against closed-form answers on a uniform box, since the
  whole point of computing them here is to agree with ``checkMesh``;
* the **dictionary and field keywords**, which are the OpenFOAM 9 spellings verified
  against that install's source and tutorials, and are easy to break silently.

Run via: mamba run -n axqua-env pytest tests/test_openfoam.py
"""

from __future__ import annotations

import numpy as np
import pytest

from axqua.solvers.openfoam import mesh as ofmesh
from axqua.solvers.openfoam import polymesh
from axqua.solvers.openfoam.quality import (
    aspect_ratios, cell_geometry, face_geometry, non_orthogonality, skewness,
)


# --------------------------------------------------------------------------- #
# fixtures: a small structured lattice, built the way build_mesh builds one
# --------------------------------------------------------------------------- #


def _plan_grid(nx: int, ny: int, dx: float = 1.0, *, holes=()) -> ofmesh.PlanGrid:
    keep = np.ones((ny, nx), dtype=bool)
    for j, i in holes:
        keep[j, i] = False
    col_id = np.full((ny, nx), -1, dtype=np.int64)
    col_id[keep] = np.arange(int(keep.sum()))
    used = np.zeros((ny + 1, nx + 1), dtype=bool)
    for dj in (0, 1):
        for di in (0, 1):
            used[dj:dj + ny, di:di + nx] |= keep
    vert_id = np.full((ny + 1, nx + 1), -1, dtype=np.int64)
    vert_id[used] = np.arange(int(used.sum()))
    cj, ci = np.nonzero(keep)
    vj, vi = np.nonzero(used)
    return ofmesh.PlanGrid(
        dx=dx, angle=0.0, origin=np.zeros(2), nx=nx, ny=ny,
        col_id=col_id, vert_id=vert_id,
        cell_xy=np.stack([(ci + 0.5) * dx, (cj + 0.5) * dx], axis=1),
        vert_xy=np.stack([vi * dx, vj * dx], axis=1),
    )


def _box(nx=3, ny=3, nz=2, dx=1.0, height=1.0, tilt=0.0):
    """A lattice extruded between a (optionally tilted) bed and a parallel lid."""
    grid = _plan_grid(nx, ny, dx)
    bed = tilt * grid.vert_xy[:, 0]
    lid = bed + height
    (points, internal, bed_face, top_face, sides, z, centres, column,
     layer) = ofmesh.extrude(grid, bed, lid, nz)
    boundary = [
        ("bed", "wall", bed_face[1], bed_face[0]),
        ("banks", "wall", sides["owner"], sides["quads"]),
        ("atmosphere", "patch", top_face[1], top_face[0]),
    ]
    mesh = polymesh.assemble(points, internal, boundary, centres)
    return grid, mesh, centres, z


# --------------------------------------------------------------------------- #
# polyMesh addressing contract
# --------------------------------------------------------------------------- #


def test_polymesh_is_structurally_valid():
    _, mesh, _, _ = _box()
    assert polymesh.validate(mesh) == []


def test_upper_triangular_ordering_and_owner_less_than_neighbour():
    _, mesh, _, _ = _box(4, 3, 3)
    ni = mesh.n_internal_faces
    owner = mesh.owner[:ni]
    assert (owner < mesh.neighbour).all()
    # ascending owner, and ascending neighbour within one owner
    key = owner.astype(np.int64) * (mesh.n_cells + 1) + mesh.neighbour
    assert (np.diff(key) > 0).all()


def test_every_face_points_from_owner_to_neighbour():
    _, mesh, centres, _ = _box(3, 3, 3)
    ni = mesh.n_internal_faces
    normals = mesh.face_normals()
    d = centres[mesh.neighbour] - centres[mesh.owner[:ni]]
    assert (np.einsum("ij,ij->i", normals[:ni], d) > 0).all()
    # boundary normals point out of the owner cell
    out = mesh.face_centres()[ni:] - centres[mesh.owner[ni:]]
    assert (np.einsum("ij,ij->i", normals[ni:], out) > 0).all()


def test_cell_and_face_counts_match_the_lattice():
    nx, ny, nz = 4, 3, 5
    grid, mesh, _, _ = _box(nx, ny, nz)
    assert mesh.n_cells == nx * ny * nz
    # internal: (nx-1)*ny + nx*(ny-1) column pairs x nz, plus nx*ny*(nz-1) horizontal
    expected_internal = ((nx - 1) * ny + nx * (ny - 1)) * nz + nx * ny * (nz - 1)
    assert mesh.n_internal_faces == expected_internal
    assert mesh.patch("bed").n_faces == nx * ny
    assert mesh.patch("atmosphere").n_faces == nx * ny
    assert mesh.patch("banks").n_faces == 2 * (nx + ny) * nz
    assert mesh.n_points == (nx + 1) * (ny + 1) * (nz + 1)


def test_a_hole_in_the_lattice_becomes_wall_faces_not_a_gap():
    """A missing column must close the mesh, not leave it open."""
    grid = _plan_grid(3, 3, holes=[(1, 1)])
    bed = np.zeros(grid.n_vertices)
    (points, internal, bed_face, top_face, sides, _, centres, _,
     _) = ofmesh.extrude(grid, bed, bed + 1.0, 2)
    mesh = polymesh.assemble(
        points, internal,
        [("bed", "wall", bed_face[1], bed_face[0]),
         ("banks", "wall", sides["owner"], sides["quads"]),
         ("atmosphere", "patch", top_face[1], top_face[0])],
        centres)
    assert polymesh.validate(mesh) == []       # the closure check catches an open cell
    assert mesh.n_cells == 8 * 2
    # the hole contributes 4 extra wall faces per layer on top of the outer ring
    assert mesh.patch("banks").n_faces == (2 * (3 + 3) + 4) * 2


def test_written_polymesh_round_trips_the_file_layout(tmp_path):
    _, mesh, _, _ = _box(2, 2, 2)
    out = polymesh.write_polymesh(mesh, tmp_path)
    for name in ("points", "faces", "owner", "neighbour", "boundary"):
        assert (out / name).is_file()
    faces = (out / "faces").read_text()
    assert f"{mesh.n_faces}\n(\n" in faces
    assert faces.count("4(") == mesh.n_faces          # every face is a quad
    boundary = (out / "boundary").read_text()
    assert "nFaces          4;" in boundary            # the 2x2 bed
    assert "type            wall;" in boundary
    # patch face ranges must be contiguous and cover the boundary exactly
    total = sum(p.n_faces for p in mesh.patches)
    assert mesh.patches[0].start_face == mesh.n_internal_faces
    assert total + mesh.n_internal_faces == mesh.n_faces


# --------------------------------------------------------------------------- #
# vertical distribution
# --------------------------------------------------------------------------- #


def test_sigma_levels_uniform():
    s = ofmesh.sigma_levels(np.array([1.0, 2.0]), 4)
    assert s.shape == (2, 5)
    np.testing.assert_allclose(s[0], [0, 0.25, 0.5, 0.75, 1.0])
    assert (s[:, -1] == 1.0).all()


def test_sigma_levels_expansion_grows_upwards():
    s = ofmesh.sigma_levels(np.array([1.0]), 4, expansion=4.0)
    thickness = np.diff(s[0])
    assert (np.diff(thickness) > 0).all()               # each layer thicker than the last
    assert thickness[-1] / thickness[0] == pytest.approx(4.0, rel=1e-6)
    assert s[0, -1] == 1.0


def test_bed_layer_is_pinned_in_absolute_thickness():
    height = np.array([2.0, 4.0])
    s = ofmesh.sigma_levels(height, 5, bed_layer=0.4)
    first = s[:, 1] * height
    np.testing.assert_allclose(first, [0.4, 0.4])
    assert (s[:, -1] == 1.0).all()


def test_bed_layer_cannot_eat_the_column():
    """A bed layer thicker than the column is capped at half of it, not clipped to 1."""
    height = np.array([0.2])
    s = ofmesh.sigma_levels(height, 4, bed_layer=5.0)
    assert s[0, 1] * height[0] == pytest.approx(0.1)    # half the column
    assert s[0, -1] == 1.0
    assert (np.diff(s[0]) > 0).all()                    # still strictly increasing


def test_levels_are_shared_between_neighbouring_columns():
    """Conformality: the extrusion must reuse vertex levels, not recompute per cell."""
    grid = _plan_grid(2, 1)
    bed = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0])      # one corner raised
    (points, _, _, _, _, z, _, _, _) = ofmesh.extrude(grid, bed, bed + 1.0, 3)
    # the shared vertex column between the two cells has exactly one set of levels
    shared = grid.vert_id[0, 1]
    assert z[shared].shape == (4,)
    # and every point of that vertex appears once in the point list
    xy = points[shared * 4:(shared + 1) * 4, :2]
    assert np.allclose(xy, xy[0])


# --------------------------------------------------------------------------- #
# geometric measures (must agree with checkMesh's definitions)
# --------------------------------------------------------------------------- #


def test_uniform_box_geometry_is_exact():
    _, mesh, _, _ = _box(3, 3, 2, dx=2.0, height=1.0)
    fc, fn = face_geometry(mesh)
    cc, vol = cell_geometry(mesh, fc, fn)
    # 2 x 2 x 0.5 cells
    np.testing.assert_allclose(vol, 2.0, rtol=1e-12)
    nonortho, mean = non_orthogonality(mesh, fn, cc)
    assert nonortho.max() == pytest.approx(0.0, abs=1e-9)     # a Cartesian box
    assert mean == pytest.approx(0.0, abs=1e-9)
    assert skewness(mesh, fc, fn, cc).max() == pytest.approx(0.0, abs=1e-9)


def test_aspect_ratio_uses_the_component_form():
    """A flat cell must report OpenFOAM's component ratio, not the milder hydraulic one."""
    _, mesh, _, _ = _box(3, 3, 1, dx=1.0, height=0.1)
    fc, fn = face_geometry(mesh)
    cc, vol = cell_geometry(mesh, fc, fn)
    ar = aspect_ratios(mesh, fn, vol)
    # 1 x 1 x 0.1 cell: sum|Sf| is 0.2 in x and y, 2.0 in z -> 10
    assert ar.max() == pytest.approx(10.0, rel=1e-9)


def test_tilted_bed_makes_the_mesh_non_orthogonal():
    _, mesh, _, _ = _box(4, 2, 3, tilt=0.5)
    fc, fn = face_geometry(mesh)
    cc, _ = cell_geometry(mesh, fc, fn)
    nonortho, _ = non_orthogonality(mesh, fn, cc)
    assert nonortho.max() > 5.0          # the bed slope shows up
    assert nonortho.max() < 70.0         # but a 1:2 slope is nowhere near severe


# --------------------------------------------------------------------------- #
# dictionaries and fields (OpenFOAM 9 spellings)
# --------------------------------------------------------------------------- #


class _Cfg:
    """Enough of a Config for the dictionary writers."""

    def __init__(self, **kw):
        from axqua.config import Hydrodynamics, OpenFoam

        self.openfoam = OpenFoam(**kw)
        self.hydrodynamics = Hydrodynamics()


def test_fvsolution_carries_the_settings_that_tame_the_air_phase():
    from axqua.solvers.openfoam.dicts import fv_solution, stages

    cfg = _Cfg()
    run = [s for s in stages(cfg) if s.name == "run"][0]
    text = fv_solution(cfg, run)
    # semi-implicit MULES is what decouples dt from the interface Courant number
    assert "MULESCorr       yes;" in text
    assert "nAlphaSubCycles 1;" in text
    assert "momentumPredictor no;" in text
    # a terrain-following mesh needs the non-orthogonal correctors
    assert "nNonOrthogonalCorrectors 2;" in text
    assert "solver          GAMG;" in text        # p_rgh on a large mesh


def test_fvconstraints_caps_the_velocity():
    from axqua.solvers.openfoam.dicts import fv_constraints

    text = fv_constraints(_Cfg(), 4.5)
    assert "type            limitVelocity;" in text
    assert "max             4.5;" in text
    assert "selectionMode   all;" in text


def test_stage_schemes_differ_in_compression_not_in_kind():
    """Stage 1 must still compress the interface, only less - OF9 puts cAlpha in the
    scheme, so dropping interfaceCompression entirely would demand a legacy
    fvSolution key and smear the surface through the whole spin-up."""
    from axqua.solvers.openfoam.dicts import fv_schemes, stages

    cfg = _Cfg()
    spinup, run = stages(cfg)
    assert "interfaceCompression vanLeer 0.5" in fv_schemes(cfg, spinup)
    assert "interfaceCompression vanLeer 1" in fv_schemes(cfg, run)
    assert spinup.max_courant < run.max_courant
    for stage in (spinup, run):
        assert "wallDist" in fv_schemes(cfg, stage)     # kOmegaSST needs it


def test_time_step_estimate_is_calibrated_against_a_measured_run():
    """The estimate must reproduce what interFoam actually settles on.

    Measured on the isar mesh at cell_size 2 m / 8 layers: dt 1.77e-3 s at Courant
    0.3 with the 4.5 m/s cap and a 0.057 m thinnest layer. The naive
    `Courant * layer / water_speed` gave 1e-1 s - fifty times too optimistic.
    """
    from axqua.solvers.openfoam.case import estimate_time_step

    class _M:
        min_layer_height = 0.057

    cfg = _Cfg(cell_size=2.0)
    dt = estimate_time_step(_M(), cfg, 4.5, courant=0.3)
    assert dt == pytest.approx(1.9e-3, rel=0.2)      # measured 1.77e-3


def test_reach_flush_time_warns_when_the_run_is_too_short():
    """A free-surface run cannot be steady before the domain has been flushed once."""
    from axqua.solvers.openfoam.case import cost_lines

    class _Grid:
        cell_xy = np.array([[0.0, 0.0], [300.0, 400.0]])   # 500 m diagonal

    class _M:
        grid = _Grid()
        min_layer_height = 0.05
        n_cells = 1000

    class _State:
        def velocity_scale(self):
            return 1.0

    cfg = _Cfg(end_time=100.0)                       # 100 s against a 500 s flush
    text = " ".join(cost_lines(_M(), _State(), cfg, 4.5))
    assert "reach flush" in text
    assert "cannot reach a steady state" in text


def test_control_dict_monitors_water_discharge_not_total_flux():
    from axqua.solvers.openfoam.dicts import control_dict, stages

    cfg = _Cfg()
    text = control_dict(cfg, stages(cfg)[1], patches=["inlet-1", "outlet-1"])
    assert "weightField     alpha.water;" in text     # water only, not water+air
    # sampled in simulated time, not per step: an adaptive dt would otherwise give a
    # history that is dense where the run is stiff and sparse where it is not
    assert "writeControl    runTime;" in text
    assert "writeInterval   1;" in text               # write_interval 10 / 10
    assert "fields          (phi);" in text
    assert "name            inlet-1;" in text
    assert "name            outlet-1;" in text
    assert "maxAlphaCo      0.9;" in text


def test_stage_activation_swaps_the_system_dicts(tmp_path):
    from axqua.solvers.openfoam.dicts import STAGED_FILES, activate

    system = tmp_path / "system"
    for stage in ("stage1-spinup", "stage2-run"):
        (system / stage).mkdir(parents=True)
        for name in STAGED_FILES:
            (system / stage / name).write_text(stage)
    activate(tmp_path, "run")
    assert (system / "controlDict").read_text() == "stage2-run"
    activate(tmp_path, "spinup")
    assert (system / "fvSolution").read_text() == "stage1-spinup"


def test_activate_regenerates_from_the_config_it_is_given(tmp_path):
    """Changing end_time and re-running must actually change the run, not just the
    message about it - the staged dicts are rewritten from the live config."""
    from axqua.solvers.openfoam.dicts import activate, write_dicts

    class _Mesh:
        inlet_patches = ["inlet-1"]
        outlet_patches = ["outlet-1"]

    cfg = _Cfg(end_time=300.0)
    write_dicts(_Mesh(), cfg, tmp_path, velocity_cap=4.0)
    # the build leaves stage 1 active, so the case is runnable straight away
    assert "endTime         30;" in (tmp_path / "system" / "controlDict").read_text()
    stored = tmp_path / "system" / "stage2-run" / "controlDict"
    assert "endTime         300;" in stored.read_text()

    cfg.openfoam.end_time = 42.0
    activate(tmp_path, "run", cfg)
    active = (tmp_path / "system" / "controlDict").read_text()
    assert "endTime         42;" in active
    # and without a cfg the stored set is used as-is
    activate(tmp_path, "spinup")
    assert "endTime         30;" in (tmp_path / "system" / "controlDict").read_text()


def test_read_patch_names_round_trips_the_boundary_file(tmp_path):
    _, mesh, _, _ = _box(2, 2, 2)
    polymesh.write_polymesh(mesh, tmp_path)
    assert polymesh.read_patch_names(tmp_path) == [p.name for p in mesh.patches]


def test_field_rendering_uses_the_verified_boundary_types():
    from axqua.solvers.openfoam.fields import render_field, scalar_list

    text = render_field("volScalarField", "alpha.water", "[0 0 0 0 0 0 0]",
                        scalar_list(np.array([0.0, 1.0])),
                        {"inlet-1": {"type": "variableHeightFlowRate",
                                     "lowerBound": "0", "upperBound": "1"}})
    assert "class       volScalarField;" in text
    assert "object      alpha.water;" in text
    assert "nonuniform List<scalar>\n2\n(" in text
    assert "type            variableHeightFlowRate;" in text


def test_alpha_is_the_submerged_fraction_of_each_cell():
    """The VOF seed must be a one-cell-sharp interface at the 2D free surface."""
    from axqua.solvers.openfoam.fields import initial_alpha

    grid = _plan_grid(1, 1)
    bed = np.zeros(grid.n_vertices)
    (_, _, _, _, _, z, centres, column, layer) = ofmesh.extrude(grid, bed, bed + 4.0, 4)

    class _M:
        pass

    m = _M()
    m.grid, m.z, m.n_cells = grid, z, 4
    m.cell_column, m.cell_layer = column, layer
    m.column_wse = np.array([2.5])          # halfway through the third 1 m layer
    m.cell_bounds = ofmesh.OpenFoamMesh.cell_bounds.fget(m)
    alpha = initial_alpha(m)
    np.testing.assert_allclose(alpha, [1.0, 1.0, 0.5, 0.0])


# --------------------------------------------------------------------------- #
# rigid lid: the air phase removed by construction
# --------------------------------------------------------------------------- #


def _rigid_mesh(*, dx=1.5, slope=0.08, min_layer=0.040):
    """A 4x4 lattice whose bed falls at a known slope, for the cost estimate."""

    class _Grid:
        pass

    grid = _Grid()
    grid.dx = dx
    grid.col_id = np.arange(16).reshape(4, 4)
    grid.cell_xy = np.array([[0.0, 0.0], [300.0, 400.0]])

    class _M:
        pass

    m = _M()
    m.grid = grid
    m.rigid_lid = True
    m.min_layer_height = min_layer
    m.n_cells = 16
    # bed falling along i at exactly `slope`
    m.column_bed = (np.arange(16) % 4) * dx * slope
    return m


def test_rigid_lid_has_no_spin_up_stage():
    """A spin-up exists to let an interface settle; a rigid lid has no interface."""
    from axqua.solvers.openfoam.dicts import stages

    assert [s.name for s in stages(_Cfg(mode="rigid-lid"))] == ["run"]
    assert [s.name for s in stages(_Cfg())] == ["spinup", "run"]


def test_rigid_lid_solves_alpha_explicitly():
    """alpha is identically 1, so the implicit MULES correction has nothing to
    correct - and measured four fifths of the run time on isar-2025."""
    from axqua.solvers.openfoam.dicts import fv_solution, stages

    cfg = _Cfg(mode="rigid-lid")
    text = fv_solution(cfg, stages(cfg)[0])
    assert "MULESCorr       no;" in text
    # the two-phase case must keep it: there dt depends on it
    vof = _Cfg()
    assert "MULESCorr       yes;" in fv_solution(vof, stages(vof)[1])


def test_rigid_lid_time_step_follows_the_terrain_slope():
    """Measured on isar-2025 (5x coarse, rigid lid): interFoam settled on 0.369 s at
    Courant 0.9. Carrying the two-phase model across - thinnest layer at the full
    water speed - predicted 0.019 s, nineteen times too pessimistic, because only
    the *vertical* component crosses a thin layer."""
    from axqua.solvers.openfoam.case import estimate_time_step, terrain_slope

    m = _rigid_mesh()
    assert terrain_slope(m) == pytest.approx(0.08, rel=0.05)
    cfg = _Cfg(mode="rigid-lid", cell_size=1.5)
    dt = estimate_time_step(m, cfg, 4.5, courant=0.9)
    assert dt == pytest.approx(0.37, rel=0.35)       # measured 0.369


def test_rigid_lid_cost_names_the_water_not_the_air():
    from axqua.solvers.openfoam.case import cost_lines

    class _State:
        def velocity_scale(self):
            return 0.94

    text = " ".join(cost_lines(_rigid_mesh(), _State(),
                               _Cfg(mode="rigid-lid", cell_size=1.5), 4.5))
    assert "there is no air phase" in text
    assert "bed slope" in text


def test_cell_size_factor_coarsens_relative_to_the_telemac_mesh(monkeypatch):
    """The point of the factor is that one number coarsens a test run without
    editing the resolution the TELEMAC case is meshed at."""
    from axqua.solvers.openfoam import mesh as m

    cfg = _Cfg(cell_size=0.5)
    cfg.mesh = None
    monkeypatch.setattr("axqua.core.geodata.nominal_channel_size",
                        lambda _cfg: 0.5)
    assert m.plan_spacing(cfg) == pytest.approx(0.5)
    cfg.openfoam.cell_size_factor = 5.0
    assert m.plan_spacing(cfg) == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# headroom: room to disagree with the 2D seed, and the check that it was enough
# --------------------------------------------------------------------------- #


class _State:
    """A State2D stand-in carrying only the two scales headroom is built from.

    The real methods are bound onto it, so this tests the shipped arithmetic rather
    than a copy of it - and does so without a 900 MB SELAFIN.
    """

    def __init__(self, speed=1.0, depth=0.5):
        from axqua.solvers.openfoam.hotstart import State2D

        self._speed, self._depth = speed, depth
        self.headroom = State2D.headroom.__get__(self)
        self.lateral_margin = State2D.lateral_margin.__get__(self)

    def velocity_scale(self, wet_depth=0.01):
        return self._speed

    def depth_scale(self, wet_depth=0.01):
        return self._depth


def _state(speed=1.0, depth=0.5):
    return _State(speed, depth)


def test_headroom_is_the_velocity_head_plus_a_depth_allowance():
    """The surface must be able to rise by what the flow could actually convert."""
    fast = _state(speed=3.0, depth=1.0)
    # 3^2/(2g) = 0.459 m of velocity head, + 0.25 x 1.0 m of depth allowance
    assert fast.headroom(0.0) == pytest.approx(0.459 + 0.25, rel=0.02)


def test_the_configured_freeboard_is_a_floor_not_a_ceiling():
    """No case may end up with LESS air than it asked for - the derivation can only
    give the surface more room, never take it away."""
    slow = _state(speed=0.5, depth=0.2)
    assert slow.headroom(0.0) < 0.5
    assert slow.headroom(0.5) == pytest.approx(0.5)


def test_lateral_margin_turns_a_surface_rise_into_plan_distance():
    """A surface free to rise is free to spread; on a 10% bank a rise of h moves the
    water line 10h, and a footprint trimmed to the 2D line would stop it with a wall."""
    s = _state(speed=3.0, depth=1.0)
    assert s.lateral_margin(0.0, bank_slope=0.1) == pytest.approx(
        s.headroom(0.0) * 10, rel=1e-6)
    assert s.lateral_margin(50.0, bank_slope=0.1) == pytest.approx(50.0)


def test_fixed_mode_and_rigid_lid_bypass_the_derivation():
    from axqua.solvers.openfoam.mesh import resolve_headroom, resolve_margin

    fast = _state(speed=3.0, depth=1.0)
    auto = _Cfg(freeboard=0.5, wet_margin=5.0)
    assert resolve_headroom(auto, fast) > 0.5          # derived
    assert resolve_margin(auto, fast) > 5.0

    fixed = _Cfg(freeboard=0.5, wet_margin=5.0, headroom_mode="fixed")
    assert resolve_headroom(fixed, fast) == pytest.approx(0.5)
    assert resolve_margin(fixed, fast) == pytest.approx(5.0)

    rigid = _Cfg(freeboard=0.5, mode="rigid-lid")
    assert resolve_headroom(rigid, fast) == pytest.approx(0.5)   # unused anyway
    assert resolve_headroom(auto, None) == pytest.approx(0.5)    # cold build


def test_surface_freedom_reads_the_monitors_and_names_the_knob(tmp_path):
    """A run that pressed against the lid is constrained by a MESH decision, and
    nothing else in the output would say so."""
    from axqua.solvers.openfoam.report import surface_freedom

    def monitor(name, values, area=1000.0):
        d = tmp_path / "postProcessing" / name / "0"
        d.mkdir(parents=True)
        (d / "surfaceFieldValue.dat").write_text(
            f"# Region type : patch x\n# Area   : {area}\n"
            "# Time areaIntegrate(alpha.water)\n"
            + "".join(f"{i}\t{v}\n" for i, v in enumerate(values)))

    monitor("lidContact", [0.0, 0.0, 12.5, 4.0])
    monitor("wallContact", [0.0, 0.0, 0.0, 0.0])

    verdict = surface_freedom(_Cfg(), tmp_path)
    assert verdict.measured and not verdict.free
    assert verdict.lid_area == pytest.approx(12.5)      # the PEAK, not the last
    assert verdict.lid_fraction == pytest.approx(0.0125)
    text = " ".join(verdict.lines(_Cfg()))
    assert "freeboard" in text and "not physical" in text


def test_the_wall_tolerance_absorbs_the_inflow_and_outflow_corners(tmp_path):
    """The domain has to end somewhere, and at the inlet and outlet the channel runs
    into that edge - so a threshold of zero would report every healthy run as boxed
    in. Judged as a fraction, which also means the same thing on a side channel and
    on a full reach."""
    from axqua.solvers.openfoam.report import surface_freedom

    def monitor(name, peak, area):
        d = tmp_path / "postProcessing" / name / "0"
        d.mkdir(parents=True)
        (d / "surfaceFieldValue.dat").write_text(
            f"# Area   : {area}\n# Time\tvalue\n0\t0.0\n1\t{peak}\n")

    monitor("lidContact", 0.0, 4000.0)
    monitor("wallContact", 14.0, 1450.0)       # ~1% of the banks patch

    verdict = surface_freedom(_Cfg(), tmp_path)
    assert verdict.wall_area == pytest.approx(14.0)
    assert not verdict.hit_wall and verdict.free
    assert "free" in " ".join(verdict.lines(_Cfg()))


def test_surface_freedom_is_honest_about_the_rigid_lid(tmp_path):
    """There the surface never had freedom to lose, so 'free' would be a lie."""
    from axqua.solvers.openfoam.report import surface_freedom

    verdict = surface_freedom(_Cfg(mode="rigid-lid"), tmp_path)
    assert verdict.prescribed
    assert "prescribed" in " ".join(verdict.lines(_Cfg(mode="rigid-lid")))


def test_no_monitors_reports_nothing_rather_than_a_clean_bill(tmp_path):
    """An unrun case must not read as 'the surface was free'."""
    from axqua.solvers.openfoam.report import surface_freedom

    verdict = surface_freedom(_Cfg(), tmp_path)
    assert not verdict.measured
    assert verdict.lines(_Cfg()) == []


def test_the_monitors_are_written_for_vof_and_skipped_under_a_rigid_lid():
    from axqua.solvers.openfoam.dicts import control_dict, stages

    cfg = _Cfg()
    text = control_dict(cfg, stages(cfg)[1], patches=["inlet-1"],
                        boundary_patches=("atmosphere", "banks"))
    assert "lidContact" in text and "wallContact" in text
    assert "areaIntegrate" in text

    rigid = _Cfg(mode="rigid-lid")
    text = control_dict(rigid, stages(rigid)[0], patches=["inlet-1"],
                        boundary_patches=("lid", "banks"))
    assert "lidContact" not in text


# --------------------------------------------------------------------------- #
# the 3D seed: a velocity profile instead of one number per column
# --------------------------------------------------------------------------- #


def _write_3d_slf(path, *, nplan=3, npoin2=4):
    """A minimal 3D SERAFIN, written the way TELEMAC-3D actually writes one.

    In particular NPLAN goes in **IPARAM(7)** and the dims record's fourth slot is
    left at 1 - verified against a real r3d.slf (iparam[6] = 5, dims[3] = 1). The
    first version of this fixture set both, which hid the fact that the reader was
    looking in the wrong place and reported every real 3D result as 2D.
    """
    import struct

    def record(payload: bytes) -> bytes:
        return struct.pack(">i", len(payload)) + payload + struct.pack(">i",
                                                                      len(payload))

    names = ["ELEVATION Z", "VELOCITY U", "VELOCITY V"]
    npoin = npoin2 * nplan
    x = np.tile(np.arange(npoin2, dtype=float), nplan)
    y = np.zeros(npoin)
    # levels 0, 1, 2 m; velocity growing with height (a sheared column)
    z = np.repeat(np.arange(nplan, dtype=float), npoin2)
    u = np.repeat(np.arange(nplan, dtype=float) + 1.0, npoin2)   # 1, 2, 3 m/s
    v = np.zeros(npoin)

    with open(path, "wb") as f:
        f.write(record(f"{'test 3d':<72}".encode() + b"SERAFIND"))
        f.write(record(np.array([len(names), 0], dtype=">i4").tobytes()))
        for name in names:
            f.write(record(f"{name:<16}{'M':<16}".encode()))
        iparam = np.zeros(10, dtype=">i4")
        iparam[6] = nplan
        iparam[9] = 1                       # a DATE record follows
        f.write(record(iparam.tobytes()))
        f.write(record(np.array([1, 1, 1, 1, 1, 1], dtype=">i4").tobytes()))
        f.write(record(np.array([1, npoin, 6, 1], dtype=">i4").tobytes()))  # NOT nplan
        f.write(record(np.ones(6, dtype=">i4").tobytes()))
        f.write(record(np.zeros(npoin, dtype=">i4").tobytes()))
        f.write(record(x.astype(">f8").tobytes()))
        f.write(record(y.astype(">f8").tobytes()))
        f.write(record(np.array([0.0], dtype=">f8").tobytes()))
        for values in (z, u, v):
            f.write(record(values.astype(">f8").tobytes()))
    return path


def test_read_slf_unrolls_a_3d_result_onto_its_plan_nodes(tmp_path):
    """SERAFIN stores a 3D field plane by plane over the same 2D mesh; leaving it
    flat would make every consumer re-derive npoin2 from the dims record."""
    from axqua.core.selafin import read_slf

    data = read_slf(_write_3d_slf(tmp_path / "r3d.slf", nplan=3, npoin2=4))
    # from IPARAM(7), not the dims record - see _write_3d_slf
    assert data["nplan"] == 3
    assert data["x"].size == 4                       # cut back to the plan nodes
    assert data["values"]["VELOCITY U"].shape == (3, 4)
    assert data["values"]["VELOCITY U"][:, 0].tolist() == [1.0, 2.0, 3.0]


def test_a_2d_result_is_untouched_by_the_3d_path(tmp_path):
    """nplan == 1 must stay exactly as it was: flat values, no reshape."""
    from axqua.core.selafin import read_slf, write_geometry

    path = write_geometry(tmp_path / "geometry.slf",
                          x=np.array([0.0, 1.0, 0.0]), y=np.array([0.0, 0.0, 1.0]),
                          ikle=np.array([[1, 2, 3]]), ipobo=np.array([1, 2, 3]),
                          bottom=np.array([1.0, 1.0, 1.0]))
    data = read_slf(path)
    assert data["nplan"] == 1
    assert data["values"]["BOTTOM"].shape == (3,)


def test_the_3d_seed_carries_the_profile_and_a_true_depth_average(tmp_path):
    """The depth-average must integrate over the levels' actual elevations, not
    average the levels - sigma layers are equal only where the depth is."""
    from axqua.solvers.openfoam.hotstart import State2D

    state = State2D.from_slf(_write_3d_slf(tmp_path / "r3d.slf"))
    assert state.has_profile
    assert state.bottom[0] == pytest.approx(0.0)
    assert state.surface[0] == pytest.approx(2.0)
    assert state.depth[0] == pytest.approx(2.0)
    # u = 1, 2, 3 over z = 0, 1, 2 -> trapezoidal mean is exactly 2.0
    assert state.u[0] == pytest.approx(2.0)


def test_sample_profile_interpolates_in_z_and_holds_the_ends(tmp_path):
    """Below the bed and above the surface there is nothing to continue, and a
    linear extrapolation there would invent a jet."""
    from axqua.solvers.openfoam.hotstart import State2D

    state = State2D.from_slf(_write_3d_slf(tmp_path / "r3d.slf"))
    xy = np.array([[0.0, 0.0]] * 4)
    uv = state.sample_profile(xy, np.array([0.0, 0.5, 2.0, 9.0]))
    assert uv[:, 0] == pytest.approx([1.0, 1.5, 3.0, 3.0])   # last one is HELD


def test_a_2d_seed_still_answers_sample_profile(tmp_path):
    """So a caller never has to ask which kind of seed it was handed."""
    from axqua.solvers.openfoam.hotstart import State2D

    flat = State2D(x=np.array([0.0, 1.0]), y=np.array([0.0, 0.0]),
                   triangles=np.zeros((0, 3), dtype=int),
                   depth=np.array([1.0, 1.0]), surface=np.array([1.0, 1.0]),
                   bottom=np.array([0.0, 0.0]), u=np.array([2.0, 2.0]),
                   v=np.array([0.0, 0.0]))
    assert not flat.has_profile
    uv = flat.sample_profile(np.array([[0.0, 0.0], [0.0, 0.0]]),
                             np.array([0.1, 0.9]))
    assert uv[:, 0] == pytest.approx([2.0, 2.0])    # same at every elevation


def test_initial_velocity_varies_over_the_depth_with_a_3d_seed(tmp_path):
    """The whole point of pre_run.dimension: 3d. Verified against a real telemac3d
    result (inn-kb08, 5 sigma planes): the seed spans 0.074 m/s at the bed to
    0.390 m/s at the surface where a 2D seed gives a uniform 0.251 m/s."""
    from axqua.solvers.openfoam.fields import initial_velocity
    from axqua.solvers.openfoam.hotstart import State2D

    state = State2D.from_slf(_write_3d_slf(tmp_path / "r3d.slf"))   # u = 1, 2, 3
    n = 3
    z = np.array([0.0, 1.0, 2.0])

    class _Grid:
        cell_xy = np.array([[0.0, 0.0]])

    class _M:
        grid = _Grid()
        n_cells = n
        cell_column = np.zeros(n, dtype=int)
        cell_centres = np.column_stack([np.zeros(n), np.zeros(n), z])
        column_uv = np.array([[2.0, 0.0]])          # the depth-averaged value

    profiled = initial_velocity(_M(), np.ones(n), state)
    assert profiled[:, 0] == pytest.approx([1.0, 2.0, 3.0])

    state.u3d = state.v3d = state.z3d = None        # same seed, depth-averaged
    flat = initial_velocity(_M(), np.ones(n), state)
    assert flat[:, 0] == pytest.approx([2.0, 2.0, 2.0])
