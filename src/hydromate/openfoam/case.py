"""Assemble the complete OpenFOAM case: mesh, fields, dictionaries.

The counterpart of :func:`hydromate.pipeline.run` for the 3D free-surface path. It
draws on exactly the same configuration the TELEMAC case was built from - the same
ROI, the same liquid boundaries, the same roughness zones, the same discharge and
the same outflow rating - so the two models are driven identically and their results
can be compared without arguing about the forcing.

What it produces, under ``<sim_dir>/openfoam/``::

    0/                alpha.water, U, p_rgh, nut, k, omega   (seeded from r2d.slf)
    constant/
      polyMesh/       points, faces, owner, neighbour, boundary
      g, transportProperties, momentumTransport
    system/
      controlDict, fvSchemes, fvSolution                     (the ACTIVE stage)
      fvConstraints, decomposeParDict
      stage1-spinup/, stage2-run/                            (both dict sets)
    case.foam                                                (ParaView marker)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from hydromate.config import Config
from hydromate.core.capabilities import refresh_markers
from hydromate.openfoam import dicts, fields, mesh as ofmesh, quality
from hydromate.openfoam.hotstart import State2D, load_hotstart
from hydromate.openfoam.polymesh import write_polymesh

log = logging.getLogger("hydromate")

# multiple of the water velocity at which the limitVelocity constraint bites
DEFAULT_VELOCITY_CAP_FACTOR = 5.0

# OpenFOAM's cell Courant number is 0.5 * sum|phi| / V * dt - a sum over *all* the
# cell's faces, so a cell with flow through more than one pair of faces reports more
# than the textbook U dt / L. Measured on the isar mesh (dt 1.77e-3 s at Courant 0.3
# where U dt / L predicted 3.8e-3), the ratio is close to 2; the estimate below
# carries it so the reported cost is the cost, not an optimistic bound.
COURANT_FACE_SUM_FACTOR = 2.0


@dataclass
class OpenFoamArtifacts:
    """Everything the build produced, mirroring :class:`hydromate.pipeline.Artifacts`."""

    case_dir: Path
    polymesh_dir: Path
    field_files: list[Path] = field(default_factory=list)
    dict_files: list[Path] = field(default_factory=list)
    mesh: object = None                 # openfoam.mesh.OpenFoamMesh
    report: object = None               # openfoam.quality.MeshReport
    discharge: float = 0.0
    inlet_discharges: dict[str, float] = field(default_factory=dict)
    outflow_stage: float | None = None
    velocity_cap: float = 0.0
    hotstart: Path | None = None
    notes: list[str] = field(default_factory=list)
    cfg: object = None                  # the Config the case was built from
    state: object = None                # the 2D hotstart it was seeded from

    def lines(self) -> list[str]:
        seed = self.hotstart.name if self.hotstart else "none (cold start, flat lid)"
        inflow = ", ".join(f"{k} {v:g} m3/s"
                           for k, v in self.inlet_discharges.items())
        outflow = (f"stage held at {self.outflow_stage:.3f} m a.s.l."
                   if self.outflow_stage is not None else "free outfall")
        out = [f"OpenFOAM case built in {self.case_dir}"]
        out.append(f"  hotstart    : {seed}")
        out.append(f"  inflow      : {inflow}")
        out.append(f"  outflow     : {outflow}")
        out.append(f"  velocity cap: {self.velocity_cap:.2f} m/s "
                   "(limitVelocity, keeps the air phase off the time step)")
        for note in self.notes:
            out.append(f"  note        : {note}")
        return out


def _inlet_discharges(of_mesh, cfg: Config, total: float) -> dict[str, float]:
    """Discharge per inlet patch.

    Each inflow line's own ``Target flow`` when the layer carries one (isar: 1.6 and
    0.8 m3/s on the two upstream branches); otherwise the total reach discharge split
    by patch face count - a width proxy, and the same rule
    :func:`hydromate.steering._prescribed_arrays` applies to the TELEMAC boundaries,
    so the two models feed the branches identically.
    """
    patches = of_mesh.inlet_patches
    if not patches:
        raise ValueError(
            "the mesh has no inlet patch: no line of boundaries.liquid_boundaries "
            "tagged 'inflow' came within the matching tolerance of the domain edge. "
            "Check that the liquid lines lie on the wetted-corridor boundary, or "
            "raise openfoam.boundary_tolerance.")
    per_line = {p: of_mesh.inlet_discharge[p] for p in patches
                if p in of_mesh.inlet_discharge}
    if len(per_line) == len(patches):
        return per_line
    counts = {p: of_mesh.polymesh.patch(p).n_faces for p in patches}
    n_total = sum(counts.values()) or 1
    return {p: total * counts[p] / n_total for p in patches}


def build_case(cfg: Config, *, state: State2D | None = None,
               hotstart: str | Path | None = None,
               case_dir: str | Path | None = None,
               discharge: float | None = None) -> OpenFoamArtifacts:
    """Build the OpenFOAM case for *cfg*.

    *state* short-circuits reading the 2D result (used by tests); *hotstart* points
    at a different ``r2d.slf`` than the config's own; *discharge* overrides
    ``boundaries.prescribed_flowrate``.
    """
    of = cfg.openfoam
    of.validate()
    case_dir = Path(case_dir) if case_dir else cfg.openfoam_case_dir
    case_dir.mkdir(parents=True, exist_ok=True)

    if state is None:
        state = load_hotstart(cfg, hotstart)

    # The same shared helper the TELEMAC scripts use, so the 3D case takes its
    # discharge and its outflow rating from one source of truth. It also synthesises
    # the rating curve when the config leaves it unset - which isar deliberately
    # does, so each scenario derives its own from that scenario's geometry.
    from hydromate.workflow import prepare_steady_inputs

    q = float(prepare_steady_inputs(cfg, discharge))
    if q <= 0:
        raise ValueError(
            "no steady discharge for the OpenFOAM build: set "
            "boundaries.prescribed_flowrate (m3/s) in case-config.yml.")

    of_mesh = ofmesh.build_mesh(cfg, state=state)
    notes = list(of_mesh.notes)
    if state is not None:
        warning = state.resolution_check(of.cell_size)
        if warning:
            notes.append(warning)

    inlets = _inlet_discharges(of_mesh, cfg, q)
    if not of_mesh.outlet_patches:
        raise ValueError(
            "the mesh has no outlet patch: no 'outflow' line of "
            "boundaries.liquid_boundaries reached the domain edge. Without one the "
            "water has nowhere to leave and the run will simply fill up.")

    if cfg.boundaries.outflow_condition == "free":
        stage = None
        notes.append("free (Neumann) outfall: the model chooses its own tailwater")
    else:
        from hydromate.pipeline import resolve_outflow_wse

        stage = float(resolve_outflow_wse(cfg, q))
        notes.append(f"outlet stage {stage:.3f} m a.s.l. from "
                     f"boundaries.outflow_condition: {cfg.boundaries.outflow_condition}"
                     " - the same prescription the TELEMAC case uses")

    cap = of.air_velocity_cap
    if cap is None:
        reference = state.velocity_scale() if state is not None \
            else cfg.hydrodynamics.initial_velocity_guess
        cap = DEFAULT_VELOCITY_CAP_FACTOR * reference
        notes.append(f"limitVelocity cap {cap:.2f} m/s "
                     f"({DEFAULT_VELOCITY_CAP_FACTOR:g}x the reach's p95 water speed)")

    polymesh_dir = write_polymesh(of_mesh.polymesh, case_dir)
    field_files = fields.write_fields(of_mesh, cfg, case_dir, state=state,
                                      outflow_stage=stage, discharges=inlets)
    dict_files = dicts.write_dicts(of_mesh, cfg, case_dir, velocity_cap=cap)
    (case_dir / "case.foam").write_text("")     # so ParaView can open the folder

    report = quality.assess(of_mesh, state=state,
                            roughness_constant=of.roughness_constant)

    art = OpenFoamArtifacts(
        case_dir=case_dir, polymesh_dir=polymesh_dir, field_files=field_files,
        dict_files=dict_files, mesh=of_mesh, report=report, discharge=q,
        inlet_discharges=inlets, outflow_stage=stage, velocity_cap=float(cap),
        hotstart=state.source if state is not None else None, notes=notes,
        cfg=cfg, state=state,
    )
    refresh_markers(cfg)      # the case folder now has an OpenFOAM mesh; say so
    for line in art.lines():
        log.info("%s", line)
    return art


def estimate_time_step(of_mesh, cfg: Config, velocity_cap: float, *,
                       courant: float | None = None) -> float:
    """Time step [s] the Courant target will settle on.

    Two things about this are not the obvious choices, and both were corrected
    against a measured run (isar: 2e-3 s at Courant 0.3, where the obvious estimate
    gave 1e-1 s - fifty times too optimistic):

    * the limiting **length** is the *thinnest* layer in the mesh, not the bed layer
      and not ``cell_size``. The bed layer is pinned thick enough to clear the
      roughness, so the layers above it are thinner wherever a column is shallow;
    * the limiting **velocity** is the ``limitVelocity`` cap, not the reach's own
      water speed. The Courant number is set by the fastest cells in the domain, and
      those are air cells sitting at the cap - which is exactly why the cap is worth
      setting, and why it also sets the cost.
    """
    courant = courant if courant is not None else cfg.openfoam.max_courant
    length = min(of_mesh.min_layer_height, cfg.openfoam.cell_size)
    return courant * length / (COURANT_FACE_SUM_FACTOR * max(velocity_cap, 1e-3))


def reach_flush_time(of_mesh, state) -> float:
    """Rough time [s] for water to traverse the modelled reach once.

    The yardstick for "how long must this run be": a free-surface run cannot be
    called steady before the domain has been flushed at least once, and comparing
    that against the configured ``end_time`` is the difference between a run that
    means something and one that merely finished.
    """
    if state is None:
        return 0.0
    xy = of_mesh.grid.cell_xy
    span = float(np.hypot(*(xy.max(axis=0) - xy.min(axis=0))))
    return span / max(state.velocity_scale(), 1e-3)


def cost_lines(of_mesh, state, cfg: Config, velocity_cap: float) -> list[str]:
    """What this run will cost, stated before anyone commits to it.

    A 3D VOF model of a whole river reach is expensive in a way a 2D model is not,
    and the expense is not obvious from the cell count alone: it is the cell count
    **times** the number of time steps, and the time step is set by the thinnest
    cell. Spelling both out here is the difference between choosing a resolution and
    discovering one.
    """
    of = cfg.openfoam
    dt = estimate_time_step(of_mesh, cfg, velocity_cap)
    steps = of.end_time / dt if dt > 0 else float("inf")
    flush = reach_flush_time(of_mesh, state)
    out = [f"  time step   : ~{dt:.2e} s at Courant {of.max_courant:g} "
           f"(set by the thinnest {of_mesh.min_layer_height:.3f} m layer at the "
           f"{velocity_cap:.1f} m/s velocity cap, not by cell_size)",
           f"  run length  : {of.end_time:g} s needs ~{steps:,.0f} time steps "
           f"on {of_mesh.n_cells:,} cells"]
    if flush:
        out.append(f"  reach flush : ~{flush:,.0f} s for water to cross the domain "
                   "once at the reach's own speed")
        if of.end_time < flush:
            out.append(f"  ! openfoam.end_time ({of.end_time:g} s) is shorter than one "
                       f"flush ({flush:,.0f} s), so the run cannot reach a steady "
                       "state. Either accept it as a transient study of the first "
                       f"{of.end_time:g} s, or shorten the modelled reach - raising "
                       "end_time to a full flush multiplies the step count above by "
                       f"{flush / max(of.end_time, 1e-9):.0f}.")
    return out


def estimate_cells(cfg: Config, *, state: State2D | None = None) -> int:
    """Cell count a build would produce, without building it.

    Cheap enough to call before committing to a resolution: it lays the plan lattice
    and multiplies by the layer count, skipping the DEM sampling and the extrusion.
    """
    polygon, _ = ofmesh._domain_polygon(
        cfg, state, domain=cfg.openfoam.domain, wet_margin=cfg.openfoam.wet_margin,
        wet_depth=cfg.hydrodynamics.wet_depth)
    angle = ofmesh.flow_angle(cfg) if cfg.openfoam.align_to_flow else 0.0
    grid = ofmesh.build_plan_grid(polygon, cfg.openfoam.cell_size, angle=angle,
                                  max_columns=cfg.openfoam.max_plan_columns)
    return int(grid.n_columns * cfg.openfoam.n_layers)


def summarise(art: OpenFoamArtifacts) -> list[str]:
    """Printable build summary: what was made, and what to run next."""
    out = list(art.lines())
    if art.report is not None:
        out.append("")
        out += art.report.lines()
    total = art.report.n_cells if art.report is not None else 0
    if art.mesh is not None and total:
        water = float(np.mean(fields.initial_alpha(art.mesh) > 0.5))
        out.append(f"  water at t=0 : {100 * water:.1f}% of cells "
                   f"({total:,} total)")
    if art.mesh is not None and art.cfg is not None:
        out.append("")
        out.append("cost:")
        out += cost_lines(art.mesh, art.state, art.cfg, art.velocity_cap)
    return out
