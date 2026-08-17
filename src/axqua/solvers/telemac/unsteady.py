"""Unsteady (quasi-steady hydrograph) TELEMAC runs, built on the converged 2D case.

The steady initial run (``preprocessing.py`` + ``initial_run.py``) establishes a
well-converged flow field and writes it to ``r2d.slf``. This module turns that into
a **hydrograph-driven** run - a natural flood wave discretised as a sequence of
steady discharges (see ``telemac2d-unsteady.md``) - **hotstarted from that steady
result**, for 2D (``telemac2d``) or, with the same forcing, 3D (``telemac3d``).

Design decisions (deliberately different from the manual-Notepad tutorial):

* **Boundaries are NOT re-defined.** axqua's generated ``boundaries.cli`` is
  already correct for the unsteady case - the inflow is ``4 5 5`` (prescribed Q,
  depth free) and the outflow ``5 4 4`` (prescribed elevation). So there is no
  ``5 5 5 -> 4 5 5`` find-and-replace and no separate ``boundaries-unsteady.cli``:
  the inflow discharge is driven by the ``LIQUID BOUNDARIES FILE`` (Q(t)) and the
  downstream water level by its prescribed elevation (constant, or the rating stage
  SL(t) at each Q when a stage-discharge rating exists). The tutorial's
  ``STAGE-DISCHARGE CURVES`` route is the alternative; we take the liquid-file route
  because it needs no ``.cli`` edit and reuses the steady boundary numbering.
* **Hotstart from the steady result** (``PREVIOUS COMPUTATION FILE : r2d.slf``,
  clock reset to zero) instead of re-seeding a dry/pre-wetted bed - the flood wave
  then propagates from an already-converged field.
* **Quasi-steady numerics**: ``NUMBER OF CORRECTIONS OF DISTRIBUTIVE SCHEMES : 2``
  (the developers' recommendation for predictor-corrector schemes on quasi-steady
  flows), and optional ``CONTROL SECTIONS`` so the run reports the flux across each
  open boundary (verifying Q_in(t) vs Q_out(t)).
* **GAIA** (bedload / suspended load) is coupled when ``morphodynamics.enabled``.

The routines recover the FRONT2 liquid-boundary numbering from the sidecar
``liquid-boundaries.json`` the build wrote (:func:`axqua.boundary.dump_liquid_boundaries`),
so no mesh rebuild is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from axqua import hydraulics
from axqua.solvers.telemac import boundary, steering
from axqua.config import Config

log = logging.getLogger("axqua")

# quasi-steady predictor-corrector corrections per time step (TELEMAC2d manual 7.2.1)
QUASI_STEADY_CORRECTIONS = 2


@dataclass
class UnsteadySetup:
    cas: Path
    liquid_boundaries: Path
    control_sections: Path | None
    gaia_cas: Path | None
    duration: float
    results: Path
    previous_computation: str
    turbulence_model: int
    n_inflows: int
    n_outflows: int


@dataclass
class Unsteady3DSetup:
    cas: Path
    liquid_boundaries: Path
    gaia_cas: Path | None
    duration: float
    n_levels: int
    dz: float
    time_step: float
    n_time_steps: int
    results3d: Path


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #

def load_hydrograph(cfg: Config) -> hydraulics.Inflow:
    """Load the inflow hydrograph Q(t) from ``boundaries.inflow`` (the full series).

    Raises with an actionable message when no series is configured or when it is a
    single constant value (a genuine hydrograph is required for an unsteady run - a
    constant discharge is the steady case).
    """
    src = cfg.boundaries.inflow
    if src is None or not Path(src).exists():
        raise ValueError(
            "no inflow hydrograph: set boundaries.inflow in case-config.yml to a "
            "time series (LfU or a CSV with a time and a Q column). An unsteady run "
            "needs a varying discharge; a constant Q is the steady case."
        )
    inflow = hydraulics.read_inflow(Path(src), steady=False)
    import numpy as np

    q = np.asarray(inflow.discharge, dtype=float)
    if inflow.times_s is None or q.size < 2 or float(np.ptp(q)) <= 1e-9:
        raise ValueError(
            f"boundaries.inflow ({Path(src).name}) is a single/constant discharge; "
            "supply a varying hydrograph (>=2 rows with changing Q) for an unsteady run."
        )
    return inflow


def _outflow_wse_fn(cfg: Config):
    """Callable Q -> downstream WSE matching the outflow condition (None if free)."""
    cond = cfg.boundaries.outflow_condition
    if cond == "free":
        return None
    if cond == "elevation":
        wse = float(cfg.boundaries.prescribed_elevation)
        return lambda q: wse
    return hydraulics.read_stage_discharge(Path(cfg.boundaries.stage_discharge))


def _load_liquids(cfg: Config) -> list[boundary.LiquidBoundary]:
    meta = cfg.model_path(cfg.liquid_boundaries_json)
    if not meta.exists():
        raise FileNotFoundError(
            f"{meta.name} not found in {cfg.model_dir}. Re-run preprocessing.py so the "
            "build serializes the liquid-boundary numbering the unsteady run needs."
        )
    return boundary.load_liquid_boundaries(meta)


def _turbulence_model(cfg: Config) -> int:
    """Turbulence model for the unsteady run, from the built geometry when readable."""
    from types import SimpleNamespace

    from axqua.core import selafin

    geom = cfg.model_path(cfg.geometry_slf)
    mesh = None
    if geom.exists():
        try:
            data = selafin.read_slf(geom)
            mesh = SimpleNamespace(x=data["x"], y=data["y"], triangles=data["ikle"])
        except Exception:  # noqa: BLE001 - fall back to the config-based estimate
            mesh = None
    return steering.select_turbulence_model(cfg, mesh)[0]


# --------------------------------------------------------------------------- #
# control sections
# --------------------------------------------------------------------------- #

def write_control_sections(cfg: Config) -> Path | None:
    """Write a CONTROL SECTIONS file: one cross-section per open-boundary segment.

    Parses the generated ``boundaries.cli`` and, for each contiguous run of inflow /
    outflow nodes (in file / IPOBO order), emits a section defined by that run's first
    and last **global** node numbers (node-id mode, ``-1``). TELEMAC then reports the
    cumulated flux across each section, so the unsteady run can be checked for mass
    balance (Q_in(t) vs Q_out(t)). Returns ``None`` if the ``.cli`` has no liquid
    nodes. (A boundary that wraps the ``.cli`` start/end is reported as two half
    sections; sum them when reading the output.)
    """
    cli = cfg.model_path(cfg.boundary_cli)
    if not cli.exists():
        raise FileNotFoundError(
            f"{cli.name} not found; run preprocessing.py before the unsteady setup.")

    # group CONSECUTIVE .cli rows (by rank, not by global node number - the latter is
    # not consecutive along a real contour) of the same liquid tag into one section,
    # keeping that run's first and last global node numbers as the section endpoints.
    runs: list[list] = []  # [kind, first_node, last_node, last_rank]
    for line in cli.read_text().splitlines():
        left, _, comment = line.partition("#")
        tag = comment.strip().lower()
        tokens = left.split()
        if tag not in ("inflow", "outflow") or len(tokens) < 2:
            continue
        n_global, rank = int(tokens[-2]), int(tokens[-1])
        if runs and runs[-1][0] == tag and runs[-1][3] == rank - 1:
            runs[-1][2], runs[-1][3] = n_global, rank   # extend the contiguous run
        else:
            runs.append([tag, n_global, n_global, rank])
    if not runs:
        return None

    counts: dict[str, int] = {}
    lines = ["# control sections generated by axqua (unsteady flux verification)",
             f"{len(runs)} -1"]
    for kind, first, last, _rank in runs:
        counts[kind] = counts.get(kind, 0) + 1
        lines.append(f"{kind}_{counts[kind]}")
        lines.append(f"{first} {last}")
    path = cfg.model_path(cfg.control_sections_file)
    path.write_text("\n".join(lines) + "\n")
    log.info("  control sections: %d cross-sections -> %s", len(runs), path.name)
    return path


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #

def _prepare_forcing(cfg: Config):
    """Shared unsteady preamble: verify the hotstart source, load boundaries + the
    hydrograph, and write the liquid-boundaries (Q(t)/SL(t)) file. Returns
    ``(liquids, inflow, liq_path, duration, wse_fn)``."""
    results_2d = cfg.model_path(cfg.results_slf)
    if not results_2d.exists():
        raise FileNotFoundError(
            f"no steady result at {results_2d}. Run preprocessing.py then initial_run.py "
            "first - the unsteady run hotstarts from that converged 2D field.")

    liquids = _load_liquids(cfg)
    inflow = load_hydrograph(cfg)
    wse_fn = _outflow_wse_fn(cfg)
    liq = steering.write_liquid_boundaries(cfg, liquids, inflow, wse_fn)
    import numpy as np

    t = np.asarray(inflow.times_s, dtype=float)
    duration = float(t[-1] - t[0]) or 3600.0
    return liquids, inflow, liq, duration, wse_fn


def build_unsteady_case(cfg: Config, *, control_sections: bool = True) -> UnsteadySetup:
    """Write the unsteady **telemac2d** case, hotstarted from the steady ``r2d.slf``.

    Reuses ``boundaries.cli`` unchanged, drives the inflow with the hydrograph liquid
    file and the outflow with its prescribed elevation / rating, adds the quasi-steady
    correction keyword and (optionally) control sections, and couples GAIA when
    ``morphodynamics.enabled``. Returns the :class:`UnsteadySetup`.
    """
    liquids, inflow, liq, duration, wse_fn = _prepare_forcing(cfg)
    import numpy as np

    q0 = float(np.asarray(inflow.discharge, dtype=float)[0])
    outflow_wse = wse_fn(q0) if wse_fn is not None else None

    sections = write_control_sections(cfg) if control_sections else None
    gaia = steering.write_gaia_cas(cfg)
    turb = _turbulence_model(cfg)

    # internal losing/gaining source regions (percolation) carry over to the
    # unsteady run - without them the exchange (and the side channel it feeds)
    # would silently vanish from the hydrograph run. No mesh here (no re-meshing
    # in the standalone script), so node counts are not re-verified; the regions
    # file is (re)written to match the region order in the .cas. The layer is only
    # needed for its internal lines, so an unreadable/absent one warns (loudly -
    # the exchange would be dropped) rather than aborting the unsteady build.
    regions: list = []
    try:
        regions = boundary.load_internal_source_regions(cfg)
    except Exception as exc:  # noqa: BLE001 - degrade, but say so
        log.warning("could not re-read %s for internal source regions (%s); the "
                    "unsteady case will carry NO internal losing/gaining exchange",
                    cfg.boundaries.liquid_boundaries, exc)
    if regions and not (cfg.gain_lose.active
                        and cfg.gain_lose.implementation == "fortran"):
        steering.write_source_regions(cfg, regions)

    cas = steering.write_cas(
        cfg, liquids, inflow_q=q0, outflow_wse=outflow_wse,
        gaia_cas=gaia.name if gaia else None,
        previous_computation=cfg.results_slf, turbulence_model=turb,
        unsteady=True, liquid_boundaries_file=liq.name, duration=duration,
        out_name=cfg.unsteady_cas_file, results_name=cfg.results_unsteady_slf,
        n_distributive_corrections=QUASI_STEADY_CORRECTIONS,
        sections_input=sections.name if sections else None,
        sections_output=cfg.sections_output_file if sections else None,
        hotstart_note="/ hotstart: continue from the converged steady result r2d.slf",
        source_regions=regions,
    )
    n_in = sum(1 for lb in liquids if lb.kind == "inflow")
    n_out = sum(1 for lb in liquids if lb.kind == "outflow")
    log.info("unsteady2d case -> %s (hydrograph %s over %.0f s, hotstart from %s)",
             cas.name, liq.name, duration, cfg.results_slf)
    return UnsteadySetup(
        cas=cas, liquid_boundaries=liq, control_sections=sections,
        gaia_cas=gaia, duration=duration, results=cfg.model_path(cfg.results_unsteady_slf),
        previous_computation=cfg.results_slf, turbulence_model=turb,
        n_inflows=n_in, n_outflows=n_out,
    )


def build_unsteady_3d_case(cfg: Config, *,
                           total_time_factor: float = 4.0,
                           out_name: str | None = None,
                           data: dict | None = None) -> Unsteady3DSetup:
    """Write the unsteady **telemac3d** case: the same hydrograph forcing on the 3D,
    non-hydrostatic mesh, hotstarted from the 2D result.

    Writes the hydrograph liquid file (as for 2D) and delegates to
    :func:`axqua.threed.build_3d_cas` with the unsteady forcing (the 3D case has
    no DURATION keyword, so the run spans the hydrograph via its step count). Couples
    GAIA when enabled. The 3D horizontal mesh should already be validated (2D
    mesh-convergence) before running this. *out_name* overrides the steering filename
    (default ``<case-name>3d-unsteady.cas``); *data* is an optional pre-read 2D
    result so several 3D variants share one read of a large results file.
    """
    from axqua.solvers.telemac import threed

    liquids, inflow, liq, duration, _ = _prepare_forcing(cfg)
    gaia = steering.write_gaia_cas(cfg)
    setup = threed.build_3d_cas(
        cfg, unsteady=True, liquid_boundaries_file=liq.name, duration=duration,
        total_time_factor=total_time_factor, gaia_cas=gaia.name if gaia else None,
        out_name=out_name or threed.cas3d_name(cfg, "-unsteady"),
        results3d_name=cfg.results3d_unsteady_slf,
        results2d_name=cfg.results2d_from_3d_unsteady_slf,
        data=data,
    )
    log.info("unsteady 3D case -> %s (hydrograph %s over %.0f s, %d sigma levels)",
             setup.cas.name, liq.name, duration, setup.n_levels)
    return Unsteady3DSetup(
        cas=setup.cas, liquid_boundaries=liq, gaia_cas=gaia, duration=duration,
        n_levels=setup.n_levels, dz=setup.dz, time_step=setup.time_step,
        n_time_steps=setup.n_time_steps,
        results3d=cfg.model_path(cfg.results3d_unsteady_slf),
    )
