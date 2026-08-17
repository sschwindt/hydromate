"""Run the cheap solver first, so the expensive one starts from an informed guess.

An OpenFOAM free-surface run of a river reach is expensive in a way a 2D
depth-averaged run is not - and almost everything it needs to know at ``t = 0`` is
something TELEMAC already answers in minutes: where the water is, how deep it is, how
fast it moves, and roughly where its surface sits. Starting ``interFoam`` without that
means meshing air that will never be wet, paying for the whole filling transient, and
doing both in the slowest model in the chain.

This module is the orchestration that makes the seed automatic rather than a
prerequisite the user has to remember. :func:`ensure_seed` returns a path to a TELEMAC
result for :func:`axqua.solvers.openfoam.hotstart.load_hotstart` to read, having
either found one, produced one, or been told not to.

Three ways it can get there, in order of preference
---------------------------------------------------
1. **An existing converged result.** ``simulation/r2d.slf`` from the real 2D study is
   a better seed than anything this module would generate, and it is already paid for.
2. **A dedicated coarse pre-run.** A copy of the case at ``pre_run.size_scale`` times
   the cell size, pre-wetted and marched at a higher Courant number, into its own
   folder. Minutes, not hours, and it exists so that a case which has never been
   through the 2D study can still go straight to OpenFOAM.
3. **Nothing**, when the user unchecks ``pre_run.enabled``. The caller then builds cold
   under a flat lid, exactly as before this module existed.

Why it lives here and not in the OpenFOAM backend
-------------------------------------------------
``tests/test_capabilities.py::test_openfoam_does_not_import_the_telemac_backend``
enforces that the two backends are siblings: OpenFOAM is *hotstarted from* a TELEMAC
result, but it reads that through the shared SERAFIN codec in
:mod:`axqua.core.selafin`, never by reaching into the other backend. Orchestration
that must drive both therefore belongs above both, next to
:mod:`axqua.workflow` - which is where it is.

The seed is an approximation, and is treated as one
---------------------------------------------------
TELEMAC is a model too, and it can be wrong. Nothing here freezes the 3D free surface
onto the 2D answer: the surface is still solved, the lid stands a freeboard above it
with room to rise, and the footprint is dilated past the wetted edge.
:func:`axqua.solvers.openfoam.report.surface_freedom` checks afterwards whether the
run ever needed more room than it was given. (The one exception is
``openfoam.mode: rigid-lid``, where the surface really is prescribed - which is the
trade that mode exists to make, and it says so.)
"""

from __future__ import annotations

import copy
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger("axqua")

# Where a seed came from. Kept as strings rather than an enum because they are printed
# in build reports and written into logs far more often than they are branched on.
EXISTING = "existing"      # the case's own converged 2D result
PRE_RUN = "pre-run"        # a dedicated coarse run made here
PRE_RUN_3D = "pre-run-3d"  # ...followed by a TELEMAC-3D run, so the seed has a profile
DISABLED = "disabled"      # pre_run.enabled is false
NONE = "none"              # nothing available; the caller builds cold


@dataclass
class SeedResult:
    """The 2D (or 3D) state an OpenFOAM case will be seeded from."""

    path: Path | None = None
    source: str = NONE
    converged: bool | None = None       # None: not judged (no listing to read)
    imbalance: float | None = None      # relative |Qin|-|Qout| / |Qin| at the seed
    runtime_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None

    def summary(self) -> list[str]:
        """Printable account of what happened, for a build report."""
        if not self.ok:
            why = ("pre_run.enabled is false" if self.source == DISABLED
                   else "no 2D result was found and no pre-run was made")
            return [f"seed: none ({why})",
                    "  the case will be built COLD under a flat lid - many times more "
                    "air cells, plus the whole filling transient to pay for in 3D."]
        how = {EXISTING: "the case's own converged 2D result",
               PRE_RUN: "a dedicated coarse TELEMAC pre-run",
               PRE_RUN_3D: "a TELEMAC-3D pre-run",
               DISABLED: "an existing 2D result (pre-run disabled)"}.get(
                   self.source, self.source)
        out = [f"seed: {self.path.name} - {how}"]
        if self.runtime_s:
            out.append(f"  pre-run took {self.runtime_s:,.0f} s")
        if self.converged is True:
            out.append(f"  flux balance reached (relative imbalance "
                       f"{self.imbalance:.2e})" if self.imbalance is not None
                       else "  flux balance reached")
        elif self.converged is False:
            out.append("  ! the 2D run had NOT reached flux balance"
                       + (f" (relative imbalance {self.imbalance:.2e})"
                          if self.imbalance is not None else "")
                       + ". The seed is still far better than a cold start, but the "
                       "surface it carries is a transient one.")
        out += [f"  {note}" for note in self.notes]
        return out


def ensure_seed(cfg, *, enabled: bool | None = None,
                validate_env: bool = True) -> SeedResult:
    """Find or produce the TELEMAC result an OpenFOAM build should be seeded from.

    Parameters
    ----------
    enabled : per-invocation override of ``openfoam.pre_run.enabled``. ``None``
        follows the config; ``False`` is the "uncheck the box for this run" path used
        by ``--no-pre-run`` and the case scripts' module-level switch.
    validate_env : passed to :func:`axqua.pipeline.run` for the pre-run.

    Never raises for an absent or unconverged seed - a cold build is a legitimate
    outcome and the caller reports it - except under ``pre_run.require: error``, where
    an unconverged pre-run is exactly what the user asked to be stopped for.
    """
    pre = cfg.openfoam.pre_run
    on = pre.enabled if enabled is None else bool(enabled)
    existing = cfg.model_path(cfg.results_slf)

    if not on:
        if existing.exists():
            return _judge(SeedResult(path=existing, source=DISABLED), cfg.model_dir,
                          cfg)
        log.info("TELEMAC pre-run disabled and no %s: building cold", existing.name)
        return SeedResult(source=DISABLED)

    if pre.reuse and existing.exists():
        log.info("seeding from the existing 2D result %s", existing)
        result = _judge(SeedResult(path=existing, source=EXISTING), cfg.model_dir, cfg)
    else:
        result = _coarse_pre_run(cfg, validate_env=validate_env)

    # The 3D extension applies to whichever 2D result we ended up with - including a
    # reused one. Hanging it off the coarse-pre-run branch alone (as it first was)
    # made `dimension: 3d` silently do nothing on every case that already had an
    # r2d.slf, which with `reuse: true` is the normal one.
    if result.ok and pre.dimension == "3d":
        result = _extend_to_3d(cfg, result)
    return result


# --------------------------------------------------------------------------- #
# the dedicated coarse pre-run
# --------------------------------------------------------------------------- #

def _coarse_pre_run(cfg, *, validate_env: bool) -> SeedResult:
    """Build and run a coarsened copy of the case purely to produce a seed.

    Deliberately the same shape as the mesh-convergence study's per-level runs
    (:func:`axqua.convergence._level_simulator`): deep-copy the config, scale the
    whole sizing field through ``mesh.size_scale``, point ``model_dir`` at a folder of
    its own, and send the build's other products to a scratch directory that is thrown
    away. A seed needs a mesh, a steering file and a result - not a DEM clip, a ground
    truth table or a calibration config.
    """
    from axqua import pipeline

    pre = cfg.openfoam.pre_run
    target = cfg.model_path(pre.directory)
    lc = copy.deepcopy(cfg)

    # Scale the WHOLE sizing field, config values and the per-zone `Max Edge Length (m)`
    # carried in the mesh-zones gpkg alike. Scaling only channel_size/floodplain_size
    # silently does nothing when the gpkg sets that field - the bug that invalidated
    # the first ering convergence study.
    lc.mesh.size_scale = cfg.mesh.size_scale * float(pre.size_scale)
    # Pre-wet the channel: this run exists to find a surface, not to reproduce the
    # production run's dry-start wetting front, and seeding it saves most of the cost.
    lc.initialization.prewet_depth = float(pre.prewet_depth)
    lc.hydrodynamics.variable_timestep = True
    lc.hydrodynamics.desired_courant = float(pre.courant)
    if pre.duration is not None:
        lc.hydrodynamics.duration = float(pre.duration)
    if pre.n_processors:
        lc.telemac.n_processors = int(pre.n_processors)
    lc.model_dir = target

    scratch = Path(tempfile.mkdtemp(prefix="axqua-prerun-"))
    lc.preprocessing_dir = scratch
    lc.postprocessing_dir = scratch
    lc.calibration_dir = scratch

    dx = cfg.mesh.channel_size * lc.mesh.size_scale
    log.info("no 2D result to seed from: running a coarse TELEMAC pre-run "
             "(channel ~%.2f m, x%.2f) into %s", dx, pre.size_scale, target)
    t0 = time.time()
    try:
        lc.ensure_dirs()
        # dry_run=True builds AND launches the solver once - which for a steady case
        # driven by DURATION is the whole run, not a smoke test.
        pipeline.run(lc, validate_env=validate_env, dry_run=True, log_to_file=False)
    except Exception as exc:  # noqa: BLE001 - a failed pre-run must not lose the build
        log.warning("the TELEMAC pre-run failed (%s: %s); the OpenFOAM case will be "
                    "built cold", type(exc).__name__, exc)
        return SeedResult(source=NONE, runtime_s=time.time() - t0,
                          notes=[f"pre-run failed: {type(exc).__name__}: {exc}"])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    runtime = time.time() - t0
    produced = lc.model_path(lc.results_slf)
    if not produced.exists():
        return SeedResult(source=NONE, runtime_s=runtime,
                          notes=[f"the pre-run produced no {lc.results_slf}"])

    result = _judge(SeedResult(path=produced, source=PRE_RUN, runtime_s=runtime),
                    target, lc)
    result.notes.insert(0, f"coarsened x{pre.size_scale:g} (channel ~{dx:.2f} m), "
                           f"pre-wetted {pre.prewet_depth:g} m, Courant "
                           f"{pre.courant:g}")
    if result.converged is False and cfg.openfoam.pre_run.require == "error":
        raise RuntimeError(
            f"the TELEMAC pre-run in {target} did not reach flux balance"
            + (f" (relative imbalance {result.imbalance:.2e})"
               if result.imbalance is not None else "")
            + ". openfoam.pre_run.require is 'error'; set it to 'warn' to seed from "
            "the transient state anyway, lengthen hydrodynamics.duration, or coarsen "
            "openfoam.pre_run.size_scale further so the run reaches steady state.")
    return result


def _judge(result: SeedResult, model_dir, cfg) -> SeedResult:
    """Read the run's listing, if there is one, to say whether the seed is steady.

    Best-effort by design: a result handed over by a user, or copied in from another
    machine, has no ``.sortie`` beside it, and "I cannot tell" (``converged=None``)
    is the honest answer there rather than a failure.
    """
    try:
        from axqua.solvers.telemac import flux_convergence, sortie

        listing = sortie.latest_sortie(Path(model_dir), cfg.cas_file)
        if listing is None:
            return result
        data = sortie.read_sortie(listing)
        imbalance = flux_convergence.relative_imbalance(data.gross_in, data.gross_out)
        if len(imbalance):
            result.imbalance = float(imbalance[-1])
            result.converged = bool(
                result.imbalance <= cfg.hydrodynamics.flux_tolerance)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not break the build
        log.debug("could not judge the seed's convergence: %s", exc)
    return result


# --------------------------------------------------------------------------- #
# the optional 3D pre-run
# --------------------------------------------------------------------------- #

# Below this many sigma planes a TELEMAC-3D run has one layer, which is a
# depth-averaged answer computed at 3D cost. 3 is the first count that has an
# interior level and therefore a profile to speak of.
MIN_USEFUL_LEVELS = 3

# Fixed step count for the 3D seed run - TELEMAC-3D has no DURATION keyword, so a
# run is bounded by steps alone. Far fewer than add3d.py's 30,000-step
# flux-convergence case: a seed does not have to reach steady state, it has to
# produce a plausible profile for OpenFOAM to develop from.
DEFAULT_3D_STEPS = 3_000


def _extend_to_3d(cfg, seed: SeedResult) -> SeedResult:
    """Follow the 2D pre-run with a hydrostatic TELEMAC-3D run.

    The point is the **vertical velocity profile**: a 2D seed necessarily starts every
    OpenFOAM column with the same velocity from bed to surface, and a 3D TELEMAC run is
    still cheap enough to do better. The 3D case is itself hotstarted from the 2D
    result, which is why this can only run after the step above.

    **It is not always possible, and the check happens before the solver.** TELEMAC-3D
    has only sigma planes, and axqua sizes their count from the depth against the
    horizontal cell size, refusing cells more than four times taller than wide. On a
    shallow reach meshed fine in plan there is no room for an interior level at all -
    isar-2025 is 0.26 m deep with 0.62 m cells and gets exactly 2 planes, i.e. one
    layer - so the run would cost 3D time to produce a depth-averaged answer. Measured
    there: it also diverges (``GRACJG ... RELATIVE PRECISION NaN``), which is what a
    single sigma layer over a wetting-drying bed tends to do. So this refuses, says
    why, and keeps the 2D seed.

    Falls back to the 2D seed on any failure - a depth-uniform seed is a mild
    approximation, not a reason to lose the build.
    """
    from axqua.core import selafin
    from axqua.solvers.telemac import threed

    try:
        from axqua.env import TelemacRuntime
        from axqua.workflow import run_solver_streaming

        # The 3D case hotstarts from the 2D result BY NAME (`FILE FOR 2D
        # CONTINUATION`), so it has to be built and run in the folder that actually
        # holds it - which is the pre-run subfolder when the seed was made here, not
        # the main model dir.
        lc = copy.copy(cfg)
        lc.model_dir = seed.path.parent

        data = selafin.read_slf(seed.path)
        vertical = threed.infer_vertical_layers(lc, data=data)
        pinned = cfg.openfoam.pre_run.n_levels
        if pinned:
            log.info("3D pre-run: %d sigma planes pinned by openfoam.pre_run.n_levels "
                     "(the mesh itself suggests %d - %s)",
                     pinned, vertical.n_levels, vertical.reason)
            vertical = replace(vertical, n_levels=int(pinned),
                               reason=f"pinned by openfoam.pre_run.n_levels "
                                      f"(inference said {vertical.n_levels})")
        if vertical.n_levels < MIN_USEFUL_LEVELS:
            note = (f"3D pre-run skipped: this mesh supports only "
                    f"{vertical.n_levels} sigma planes ({vertical.reason}), which is "
                    "one layer - a depth-averaged answer at 3D cost. Seeding from 2D.")
            log.warning("%s", note)
            seed.notes.append(note)
            return seed

        _make_3d_robust(lc)
        # build_3d_cas, not build_3d_cases: the seed needs exactly one case - the
        # cheap hydrostatic one - and it needs it cold-started, which the three-case
        # helper (written for add3d.py's continuation workflow) does not offer.
        case = threed.build_3d_cas(
            lc, n_levels=vertical.n_levels, non_hydrostatic=False,
            hotstart="constant_depth", data=data,
            out_name=threed.HYDROSTATIC_CAS,
            results3d_name=threed.HYDROSTATIC_RESULT_3D,
            results2d_name=threed.HYDROSTATIC_RESULT_2D,
            n_time_steps=DEFAULT_3D_STEPS,
            listing_period=threed.HYDROSTATIC_LISTING_PERIOD)
        runtime = TelemacRuntime(cfg.telemac)
        # The hydrostatic case is the cheap one and the only one written with a
        # listing period short enough to see its fluxes converge; the profile it
        # gives is what the seed is after, not the non-hydrostatic detail.
        run_solver_streaming(runtime, lc, cas_file=Path(case.cas).name,
                             solver="telemac3d",
                             duration=case.time_step * case.n_time_steps)
        produced = lc.model_path(threed.HYDROSTATIC_RESULT_3D)
        if produced.exists() and _is_finite(produced):
            seed.path = produced
            # not PRE_RUN: the 2D result may well have been reused, and only the 3D
            # run is new. Saying "a dedicated coarse pre-run" there would claim work
            # that did not happen.
            seed.source = PRE_RUN_3D
            seed.notes.append(
                f"3D pre-run: {vertical.n_levels} sigma planes, so the seed carries a "
                "vertical velocity profile rather than one depth-averaged value")
            return seed
        why = ("diverged (its result is not finite)" if produced.exists()
               else f"produced no {produced.name}")
        seed.notes.append(f"the 3D pre-run {why}; seeding from 2D")
        log.warning("the 3D pre-run %s; seeding from the 2D result", why)
    except Exception as exc:  # noqa: BLE001 - 2D is a perfectly good seed
        log.warning("the 3D pre-run failed (%s: %s); seeding from the 2D result",
                    type(exc).__name__, exc)
        seed.notes.append(f"3D pre-run failed ({type(exc).__name__}); seeding from 2D")
    return seed


def _is_finite(result: Path) -> bool:
    """Whether a solver result is usable at all.

    A diverged TELEMAC run does not necessarily fail loudly - it can march to the end
    and write a file full of NaN. Seeding an OpenFOAM case from that would put NaN
    into ``U`` at t=0 and produce a run that dies with no obvious cause, so the seed
    is checked before it is adopted rather than after it has done damage.
    """
    try:
        import numpy as np

        from axqua.core import selafin

        values = selafin.read_slf(result)["values"]
        return all(np.isfinite(v).all() for v in values.values())
    except Exception as exc:  # noqa: BLE001 - unreadable is also unusable
        log.debug("could not validate %s: %s", result, exc)
        return False


def _make_3d_robust(lc) -> None:
    """Two overrides the 3D *seed* run needs, both measured on isar-2025.

    **Do not hotstart from the 2D continuation.** `FILE FOR 2D CONTINUATION` lifts the
    2D free surface onto the sigma mesh, and on a braided reach that surface comes with
    a great many dry and near-dry columns. TELEMAC-3D diverges on the very first solve
    there (``GRACJG ... RELATIVE PRECISION NaN`` immediately after ``DEPTH IS READ IN
    THE BINARY FILE``), on the coarse pre-run mesh and on the fine production mesh
    alike. A ``CONSTANT DEPTH`` cold start at the reach's own median depth runs clean.
    This is the same choice the vertical-convergence study already makes, for the same
    reason - and it costs nothing here, because a seed does not need the 3D run to
    agree with the 2D one about the surface. It needs a plausible *profile*.

    **Do not take Spalart-Allmaras into 3D.** The 2D closure is auto-selected and on a
    coarse mesh that is S-A, which maps to 3D model 5 - where it diverges in the
    velocity-diffusion solve on a wetting/drying bed unless a source-term patch is
    linked in (see any case's ``user_fortran/README``). Measured: NaN with S-A on two
    different meshes, clean with k-epsilon. A seed is not a turbulence study, so the
    robust closure is the right one to spend the run on.
    """
    from axqua.solvers.telemac import steering

    try:
        pick = steering.select_turbulence_model(lc)[0]
    except Exception as exc:  # noqa: BLE001 - the override is a safety net, not a need
        log.debug("could not read the turbulence pick: %s", exc)
        return
    if int(pick) == 6:                      # 2D Spalart-Allmaras -> 3D model 5
        log.info("3D pre-run: using k-epsilon instead of Spalart-Allmaras, which "
                 "diverges in 3D on a wetting/drying bed without a source-term patch")
        lc.hydrodynamics.turbulence_model = 3
