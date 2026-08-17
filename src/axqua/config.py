"""User configuration framework for the TELEMAC setup pipeline.

A single YAML file (see ``cases/example-Inn/case-config.yml``) describes the whole case: where the
TELEMAC environment lives, the coordinate system, the input geodata/hydraulics,
the meshing and friction strategy, and the calibration parameters handed to
HydroBayesCal. This module loads that YAML into validated dataclasses.

Design goals:
* one source of truth for paths so every stage agrees on file locations;
* sane defaults so a minimal YAML works, but every knob is overridable;
* fail loudly and early (missing inputs, inconsistent CRS) before TELEMAC runs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from axqua.core import schema

log = logging.getLogger("axqua")

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


#: What the artifact folder was called before the project was renamed to aXqua.
LEGACY_SIM_DIR = "hydromate-case"
DEFAULT_SIM_DIR = "axqua-case"


def _resolve_sim_dir(cfg_dir: Path, configured: str | None) -> str:
    """The artifact folder, honouring one that predates the rename.

    New cases get ``axqua-case/``. But a case built before the rename has its results in
    ``hydromate-case/`` - possibly tens of gigabytes of them - and silently pointing at a
    fresh empty folder would present as a case that had never been built. So when the
    configured (or default) folder is **absent** and the old one is **present**, the old
    one wins, and says so.

    Deliberately narrow: it fires only when the new folder does not exist, so it can never
    override a case that has genuinely been migrated, and it never creates anything.
    """
    name = str(configured or DEFAULT_SIM_DIR)
    if name == LEGACY_SIM_DIR:
        return name
    target = _resolve(cfg_dir, name)
    legacy = _resolve(cfg_dir, LEGACY_SIM_DIR)
    if target is not None and not target.exists() and legacy is not None and legacy.is_dir():
        log.warning(
            "using the pre-rename artifact folder %s/ (there is no %s/ yet). Rename it "
            "when convenient - it is a plain move on the same filesystem - or set "
            "project.sim_dir explicitly.", legacy.name, target.name)
        return LEGACY_SIM_DIR
    return name


def _resolve(base: Path, value: str | os.PathLike | None) -> Path | None:
    """Resolve *value* relative to *base* unless it is already absolute."""
    if value is None:
        return None
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _only_known(cls, data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys that are not dataclass fields of *cls* (forward-compatible)."""
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown config keys {sorted(unknown)}")
    return {k: v for k, v in data.items() if k in known}


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


@dataclass
class Environment:
    """How to enter a solver's installation on *this* machine.

    Optional everywhere: a config that only carries ``telemac.pysource`` (every case
    written before this existed) resolves to a POSIX environment against that script,
    so nothing has to be migrated. See :mod:`axqua.core.environment`.

    ``kind`` is what makes Windows work. TELEMAC has native Windows builds configured
    through a ``.bat`` (``windows``); the Foundation OpenFOAM axqua targets has no
    native Windows build, so on Windows it is reached through ``wsl``.
    """

    kind: str | None = None            # posix | windows | wsl (default: match the host)
    setup_script: Path | None = None   # overrides telemac.pysource / openfoam.bashrc
    shell: str | None = None           # a bundled bash (blueCFD / ESI / TELEMAC-Windows)
    distro: str | None = None          # WSL only
    mpi_launcher: str | None = None    # mpirun (default) | mpiexec (MS-MPI) | srun
    overrides: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if self.kind is not None and self.kind not in ("posix", "windows", "wsl"):
            raise ValueError(
                f"environment.kind must be 'posix', 'windows' or 'wsl', "
                f"got {self.kind!r}")
        if self.distro and self.kind != "wsl":
            raise ValueError("environment.distro only applies to kind: wsl")


@dataclass
class TelemacEnv:
    """How to reach and run the TELEMAC solver.

    ``pysource`` is the shell script that, when sourced, exports HOMETEL and the
    TELEMAC PYTHONPATH (e.g. ``configs/pysource.mint22.sh``). The pipeline does
    not import TELEMAC's Python directly; it sources this script in a subshell
    for any solver call (see :mod:`axqua.env`).
    """

    pysource: Path
    # how to enter that installation (kind/shell/distro/mpi_launcher); omitted means
    # a POSIX shell sourcing `pysource`, which is what every existing case wants
    environment: Environment = field(default_factory=Environment)
    solver: str = "telemac2d"  # telemac2d | telemac3d
    n_processors: int = 1
    version: str = "v8p4"
    # Python used to RUN the TELEMAC launcher (telemac2d.py / telemac3d.py). Those
    # scripts import numpy, which the bare pysource shell does not necessarily
    # provide. Left unset, axqua runs them with **its own interpreter**
    # (``sys.executable``) - which has numpy by construction, since axqua needs
    # it - while the sourced pysource still supplies HOMETEL and the TELEMAC
    # PYTHONPATH. Set this only to use a *different* interpreter than the one
    # axqua runs in; ``AXQUA_TELEMAC_PYTHON`` overrides it per machine
    # without editing the config.
    solver_python: str | None = None

    def validate(self) -> None:
        if not Path(self.pysource).is_file():
            raise FileNotFoundError(
                f"TELEMAC pysource script not found: {self.pysource}. "
                "Set telemac.pysource to e.g. "
                "/home/schwindt/opt/telemac/configs/pysource.mint22.sh"
            )
        if self.solver not in ("telemac2d", "telemac3d"):
            raise ValueError(f"telemac.solver must be telemac2d|telemac3d, got {self.solver!r}")


@dataclass
class GroundTruthSource:
    """One raw ground-truth source to compile into the tidy measurements table.

    Measured values and their positions usually live in separate files: the
    *values* export (e.g. a SonTek FlowTracker ``.ft.sum`` workbook) and a
    *positions* point layer (shp/gpkg) joined on ``join_key``. ``category`` is the
    tab the source contributes to the tidy table (``hydraulics``, ``sediment``,
    ...); ``kind`` selects the adapter (``flowtracker`` or ``points``).
    """

    category: str
    kind: str = "flowtracker"            # flowtracker | points
    values: Path | None = None           # measured-values file (e.g. .ft.sum xlsx)
    positions: Path | None = None        # position layer (shp/gpkg) to join coords from
    join_key: str = "ID"


@dataclass
class CalibrationTargets:
    """The user-filled ``calibration-target-data.xlsx`` template and its links.

    ``file`` is the filled template (generated by ``axqua targets``; tabs
    ``hydraulics`` / ``morphodynamics`` / ``parameters``). Measurement rows are
    joined by their unique ``join_key`` (default ``ID``) to the per-category
    point layers in ``user-sources/geodata/`` - ``hydraulics_positions`` and
    ``sediment_positions`` (reprojected to the project CRS on ingest); rows may
    instead carry explicit x/y, which win over the layer.
    """

    file: Path | None = None                  # filled calibration-target-data.xlsx
    hydraulics_positions: Path | None = None  # point layer for the hydraulics tab IDs
    sediment_positions: Path | None = None    # point layer for the morphodynamics tab IDs
    join_key: str = "ID"


@dataclass
class Geodata:
    """User-provided geodata files (paths resolved against the config dir).

    Pure geodata only - the boundary *conditions* (liquid boundary lines, inflow,
    rating curve, prescribed values) live in :class:`Boundaries`, and the
    calibration ground truth in :class:`GroundTruth`.
    """

    dem_initial: Path                    # baseline terrain (GeoTIFF)
    boundary: Path                       # ROI / max wetted extent polygon (shp/gpkg)
    dem_target: Path | None = None       # optional 2nd DEM for morphodynamic target
    breaklines: Path | None = None       # internal constraint lines (shp/gpkg)
    mesh_zones: Path | None = None       # polygons with a 'Zone Name' (channel/floodplain) for sizing
    channel_centerline: Path | None = None  # line the channel cells are elongated along
    roughness_zones: Path | None = None
    # dams / weirs / walls / buildings (polygons, or lines + a width)
    structures: Path | None = None  # polygons with a 'Zone ID' for per-node roughness
    roughness_table: Path | None = None  # csv: zone_id, roughness (e.g. ks) -> calibrated by HBC
    region_points: Path | None = None    # seed points carrying MATID + max area
    region_table: Path | None = None     # region-pts-table.txt (MATID legend)
    dem_of_difference: Path | None = None  # precomputed DoD (else derived from DEMs)
    # line layer of internal cross-sections ("baffles"): the discharge across each is
    # integrated from the result file after a run (axqua.sections), so a braided
    # reach can be split into its threads. Any CRS - reprojected on read.
    control_sections: Path | None = None
    control_section_name_field: str | None = None  # attribute holding each line's name

    def validate(self) -> None:
        required = {"dem_initial": self.dem_initial, "boundary": self.boundary}
        missing = [name for name, p in required.items() if p is None or not Path(p).exists()]
        if missing:
            raise FileNotFoundError(f"Missing required geodata: {missing}")
        for name in ("dem_target", "breaklines", "mesh_zones", "channel_centerline",
                     "roughness_zones", "roughness_table", "region_points",
                     "region_table", "dem_of_difference", "control_sections"):
            p = getattr(self, name)
            if p is not None and not Path(p).exists():
                raise FileNotFoundError(f"geodata.{name} set but not found: {p}")


@dataclass
class Structures:
    """Dams, weirs, walls and buildings (optional; off unless a layer is given).

    Deliberately expressed as an ordinary vector layer with attributes rather than a
    triangulated surface: axqua writes the mesh itself, so a structure only has
    to say where its footprint is and how high it stands. No STL, no CAD step, and
    nothing QGIS cannot author or round-trip. See :mod:`axqua.core.structures`.
    """

    enabled: bool = True          # ignored unless geodata.structures is set
    # attribute names; each is looked up case-insensitively with sensible aliases
    type_field: str = "Type"          # dam / weir / wall / building / embankment ...
    mode_field: str = "Mode"          # optional explicit override: solid | overflow
    crest_field: str = "Crest (m)"    # LEVEL crest elevation [m a.s.l.]
    height_field: str = "Height (m)"  # or: height above the local ground [m]
    width_field: str = "Width (m)"    # line features only: buffer width [m]
    name_field: str = "Name"
    default_width: float = 1.0        # [m] when a line carries no width
    # For a solid structure in a 2D depth-averaged model there is no vertical wall to
    # remove, so the bed is raised to the crest plus this freeboard instead - the
    # standard practice for a never-overtopped wall in TELEMAC.
    solid_freeboard_2d: float = 2.0

    def validate(self) -> None:
        if self.default_width <= 0:
            raise ValueError("structures.default_width must be > 0")


@dataclass
class Boundaries:
    """Boundary conditions: the liquid-boundary lines plus the prescribed values.

    ``liquid_boundaries`` is the LINE layer whose 'Type (inflow/outflow)' field
    tags each line; it is required for a build. ``inflow`` (a discharge time series
    CSV) is OPTIONAL - it drives a later *unsteady* case; the steady initial run is
    driven by the scalar ``prescribed_flowrate`` (and ``prescribed_elevation`` /
    ``stage_discharge`` for the outflow).
    """

    liquid_boundaries: Path | None = None  # inflow/outflow boundary lines (shp/gpkg)
    inflow: Path | None = None           # discharge time series (csv); optional, for unsteady
    stage_discharge: Path | None = None  # outlet rating curve (csv), for outflow_condition=stage_discharge
    # downstream (outflow) boundary type:
    #   "elevation"       -> prescribed water level (cli 5 4 4) from prescribed_elevation (default)
    #   "stage_discharge" -> prescribed water level (cli 5 4 4) read from stage_discharge at Q
    #   "free"            -> Neumann / free outflow (cli 4 4 4), nothing prescribed
    outflow_condition: str = "elevation"
    # how a MISSING stage_discharge rating is synthesised for outflow_condition
    # "stage_discharge":
    #   "trapezoid" -> normal depth in a trapezoid as wide as the outflow line
    #   "section"   -> conveyance of the ACTUAL DEM cross-section along that line,
    #                  with the ks of the roughness zone each sample falls in.
    # The trapezoid puts the full width at the full depth, so on a natural V-shaped
    # section it over-estimates the flow area and returns a stage that is too low;
    # the outflow boundary then holds the level below the reach's own normal depth
    # and the flow accelerates through the outlet (a supercritical drawdown) to pass
    # the discharge through the too-small section.
    rating_method: str = "trapezoid"
    prescribed_flowrate: float | None = None   # upstream Q (m3/s) for the steady run
    prescribed_elevation: float | None = None  # downstream WSE (m) for outflow_condition=elevation
    # internal ('int-*') losing/gaining lines become TELEMAC SOURCE REGIONS: each
    # line is buffered to a strip this wide (m) so the exchange discharge spreads
    # over many cells (Q/area as a gentle depth rate) instead of hammering the few
    # nodes under the line - a sink concentrated on single nodes dries them, spikes
    # velocities and collapses the CFL-adaptive time step (TELEMAC has no depth
    # guard on negative sources).
    internal_source_region_width: float = 3.0

    def validate(self) -> None:
        if self.liquid_boundaries is None or not Path(self.liquid_boundaries).exists():
            raise FileNotFoundError(
                f"boundaries.liquid_boundaries not found: {self.liquid_boundaries}"
            )
        for name in ("inflow", "stage_discharge"):
            p = getattr(self, name)
            if p is not None and not Path(p).exists():
                raise FileNotFoundError(f"boundaries.{name} set but not found: {p}")
        if self.outflow_condition not in ("stage_discharge", "elevation", "free"):
            raise ValueError(
                "boundaries.outflow_condition must be 'stage_discharge', 'elevation', "
                f"or 'free', got {self.outflow_condition!r}"
            )
        if self.rating_method not in ("trapezoid", "section"):
            raise ValueError("boundaries.rating_method must be 'trapezoid' or "
                             f"'section', got {self.rating_method!r}")


@dataclass
class Initialization:
    """Initial-condition controls for the run (dry start vs pre-wetting)."""

    initial_conditions: str = "ZERO DEPTH"
    # DRY START (the default): a fully dry bed makes TELEMAC's DEBIMP abort at a
    # prescribed-Q inflow, so instead we seed only a thin water plug on the nodes
    # within dry_start_extent of the inflow line(s) - deep enough (dry_start_depth,
    # kept >= the normal depth so the inflow stays subcritical) for the discharge to
    # establish - and leave the rest of the domain DRY. The flow then wets the reach.
    dry_start_depth: float = 0.5          # inflow-plug seed depth [m]
    dry_start_extent: float | None = None  # plug buffer around the inflow line [m]; default ~5 cells
    # pre-wetting (hotstart): when set, instead of the dry start above, seed this
    # water depth (m) on the *channel mesh-zone* nodes (dry elsewhere) and continue
    # from it - wets the whole low-flow channel up front (the mesh-convergence study
    # uses it to skip advancing the wetting front per mesh). Needs geodata.mesh_zones.
    prewet_depth: float | None = None
    # HOW the pre-wet water surface is built (pre-wetting is enabled by prewet_depth):
    #   "normal-depth" (default) - cut a cross-section every prewet_bin_spacing along
    #       the channel centerline and seed the surface at the NORMAL-FLOW stage of
    #       that section at the case discharge (rating.stage_for_discharge), scaled by
    #       prewet_fill. The seeded surface then tracks the surface the run converges
    #       to, so no water is laid down above it.
    #   "constant" - the older seed: a longitudinally smoothed low percentile of the
    #       bed plus prewet_depth. On a braided reach that percentile sits well above
    #       the thalweg, so the seeded surface lands a median 0.28 m ABOVE the
    #       converged one (measured on isar-2025), flooding bar tops and bank shelves.
    #       The excess drains only where it can flow; on flat ground over coarse gravel
    #       it stops as a 1-5 cm immobile film - 4694 m2 (35 % of the wetted area) that
    #       had stopped shrinking by t=5000 s. Used automatically without a centerline.
    prewet_mode: str = "normal-depth"
    # Fraction of the normal depth above the thalweg to seed (normal-depth mode).
    # DELIBERATELY BELOW 1: under-seeding is recoverable (the flow refills a pool in
    # seconds), over-seeding is not (a perched film never leaves - a 2D model has no
    # infiltration or evaporation). Calibrated against the converged isar-2025 result:
    # 1.00 seeds a median +0.13 m too high, 0.70 lands at -0.03 m (p90 +0.21 m) and
    # seeds 8294 m2 against the 7101 m2 that actually carries flow.
    prewet_fill: float = 0.70
    # Never seed a film thinner than this (m): a feathered seed margin is exactly what
    # becomes stagnant film, and it carries no flow worth hotstarting.
    prewet_min_depth: float = 0.05
    prewet_bin_spacing: float = 20.0          # spacing of the seed cross-sections [m]
    prewet_transect_half_width: float = 60.0  # half-width of each seed cross-section [m]
    # Measure the pre-wet depth from each node's SPILL elevation instead of its bed,
    # so no water is seeded below the rim of a closed depression. A 2D model has no
    # infiltration and no evaporation, so water seeded into a bowl the flow would
    # never fill can never leave: it survives as a stagnant, wrongly wetted alcove
    # or side channel for the whole run, and a longer run cannot repair it. On
    # freely draining ground the spill elevation equals the bed, so the open channel
    # is seeded exactly as before. See steering.spill_elevations.
    drainable_prewet: bool = True
    # bed-elevation allowance (m) when deciding that a node drains freely: a node is
    # seeded when its spill elevation is within this of its own bed. Absorbs DEM and
    # mesh-interpolation noise; the water that can still be trapped per node is at
    # most this depth.
    drainable_tolerance: float = 0.02

    def validate(self) -> None:
        if self.prewet_mode not in ("normal-depth", "constant"):
            raise ValueError("initialization.prewet_mode must be 'normal-depth' or "
                             f"'constant', got {self.prewet_mode!r}")
        if not 0.0 < self.prewet_fill <= 1.0:
            raise ValueError("initialization.prewet_fill must be in (0, 1], got "
                             f"{self.prewet_fill!r}")
        if self.prewet_min_depth < 0.0:
            raise ValueError("initialization.prewet_min_depth must be >= 0, got "
                             f"{self.prewet_min_depth!r}")
        if self.prewet_bin_spacing <= 0.0 or self.prewet_transect_half_width <= 0.0:
            raise ValueError(
                "initialization.prewet_bin_spacing and prewet_transect_half_width "
                "must be positive"
            )


@dataclass
class GainLose:
    """A **gain-lose reach**: flow that leaves the surface through a porous body and
    returns to it downstream.

    A 2D depth-averaged model has no subsurface, so the underflow is represented as
    an internal withdrawal where water infiltrates plus an injection where it
    resurfaces. Set ``enabled: true`` and point ``zone`` at the porous body (a gravel
    bar, an alluvial patch); everything else has a working default.

    **Where the exchange happens** is ``faces``:

    * ``water-table`` (default): the faces follow the physics. The bar's saturated
      zone is bounded by the two channel levels it exchanges with, so the water table
      (:mod:`axqua.watertable`) is known, and then a node **loses** where it is
      wet and its free surface stands above the table, and **gains** where the table
      stands above the bed. Nothing has to be drawn, and the faces move with the
      stage instead of being frozen at build time - which matters, because it is
      genuinely fuzzy where percolation begins and ends.
    * ``lines``: the exchange is pinned to the ``int-*`` lines of
      ``boundaries.liquid_boundaries``, each buffered to a strip
      (``boundaries.internal_source_region_width``). Pick this when the exchange
      location is actually known - a spring line, a measured seepage face.

    **How big it is** is ``conductivity`` or ``discharge``:

    * ``conductivity`` (kf) drives it by default: the exchange follows Green-Ampt's
      saturated limit at the local head, so it responds to water level and kf is a
      physically meaningful **calibration parameter** (HydroBayesCal knows it as
      ``gainlose_kf``).
    * ``discharge`` overrides that with a measured total [m3/s], normalised over the
      losing face - available with either ``faces`` setting.

    ``mode`` selects the implementation and defaults to ``fortran`` when enabled:
    a generated ``USER_RAIN`` routine that withdraws **depth-limited** (tapering to
    zero as a cell approaches ``min_depth``, so a sink can never dry a cell) and
    reinjects exactly what it took. ``region`` uses TELEMAC SOURCE REGIONS instead -
    simpler, no compiler needed, but a fixed-rate sink diverges as the local depth
    approaches zero (see cases/isar-2025/test-approaches.md, rungs T2/T3).

    The zone layer may carry a porous-depth attribute (``depth_field``) and a name
    (``name_field``).
    """

    enabled: bool = False
    zone: Path | None = None       # the porous body: polygon layer (gpkg/shp)
    # WHERE the exchange takes place: derived from the water table, or pinned to the
    # int-* lines. See the class docstring - "water-table" needs only `zone`.
    faces: str = "water-table"     # water-table | lines
    # Measured total exchange [m3/s]. Overrides `conductivity` when set; normalised
    # over the losing face. Left None, the exchange is kf-driven and responds to
    # stage. With faces: lines and no value here, the int-* lines' own flow column is
    # used (the original spelling).
    discharge: float | None = None
    # How far the losing/gaining faces may reach OUTSIDE the zone polygon [m]. The
    # polygon usually outlines the bar itself, while the water it loses is in the
    # channel beside it, so the faces need to reach the adjacent wetted edge. None
    # -> one channel cell.
    zone_buffer: float | None = None
    # HOW the exchange is applied. Left "off", `enabled: true` selects "fortran" -
    # the depth-limited, mass-exact USER_RAIN routine, which is what a gain-lose
    # reach should use. Set it explicitly only to pick "region" (TELEMAC SOURCE
    # REGIONS: no compiler needed, but a fixed-rate sink diverges as the local depth
    # goes to zero - see test-approaches.md T2/T3). The default stays "off" so a case
    # that carries int-* lines but no gain_lose block behaves exactly as before.
    mode: str = "off"              # off | region | fortran
    # WHERE the surface water is withdrawn from. This is a PHYSICAL choice, not a
    # numerical one - see cases/isar-2025/test-approaches.md.
    #   "line"  - a strip of boundaries.internal_source_region_width around the
    #             losing LINE. Correct when the water percolates BENEATH a patch
    #             that is dry on top: the surface loses water where it infiltrates
    #             (the upstream edge of the patch, i.e. the line), then the flow
    #             travels underground and resurfaces at the gaining line. The strip
    #             must be wide enough that its WET area can supply the discharge -
    #             on the Isar reach 12 m gives ~178 m2 -> 0.36 mm/s for
    #             0.065 m3/s, while 3 m gives only 42 m2 -> 1.54 mm/s (over
    #             max_rate).
    #   "patch" - the whole percolation polygon. Only meaningful when the patch is
    #             SUBMERGED, so there is surface water across it to withdraw. If
    #             the patch is dry on top (the Isar case), this takes water that is
    #             not there: the run stays stable but the exchange decays to almost
    #             nothing and no water reaches the gaining line.
    losing_region: str = "line"
    depth_field: str = "porous depth (m)"   # attribute naming the porous depth
    name_field: str = "Patch name"          # attribute naming each patch
    min_depth: float = 0.05    # [m] never extract below this water depth (fortran)
    taper_depth: float = 0.05  # [m] linear taper band above min_depth (fortran)
    # SATURATED HYDRAULIC CONDUCTIVITY k_f [m/s] of the porous body - the parameter
    # that sets how much water the reach exchanges. The withdrawal follows
    # Green-Ampt's saturated limit,
    #     f = k_f * (h + Lz + hf) / Lz   [m/s] per node,
    # with Lz the porous depth and hf the wetting-front suction, so the exchange
    # RESPONDS to water level instead of being fixed. Set `discharge` instead to
    # prescribe a measured total and ignore k_f.
    #
    # WHERE TO LOOK VALUES UP - riverbed conductivity is not a textbook constant; it
    # spans orders of magnitude with grain size and colmation, and varies within one
    # site. The standard pooled compilation is
    #     Calver, A. (2001), "Riverbed Permeabilities: Information from Pooled Data",
    #     Ground Water 39(4), 546-553.  doi:10.1111/j.1745-6584.2001.tb02343.x
    #     https://ngwa.onlinelibrary.wiley.com/doi/10.1111/j.1745-6584.2001.tb02343.x
    # which assembles published riverbed values over 1e-9 .. 1e-2 m/s, concentrated
    # in 1e-7 .. 1e-3 m/s. As a starting bracket for a gravel bed:
    #     clean open-framework gravel   1e-2 .. 1e-1
    #     moderately colmated gravel    1e-4 .. 1e-3     <- typical alluvial bar
    #     strongly colmated / silted    1e-7 .. 1e-5
    # Because the plausible range is this wide, k_f is exposed to HydroBayesCal as
    # the calibration parameter `gainlose_kf` (see targets.PARAMETER_CATALOG): pick a
    # bracket from the reference and let the calibration find the value.
    #
    # NB TELEMAC's own Green-Ampt (RAINFALL-RUNOFF MODEL 3) cannot be used for this:
    # it abstracts infiltration from RAINFALL only (no rain -> no infiltration) and
    # is a pure sink with no way to return the water. Only its formula is reused.
    conductivity: float | None = None   # k_f [m/s]; ~1e-4..1e-3 for a colmated gravel bed
    suction: float = 0.2                # hf [m], Green-Ampt wetting-front suction
    porous_depth: float = 0.5           # Lz [m] fallback when the patch layer has no field
    # [m/s] absolute ceiling on the local withdrawal rate (fortran). The routine
    # normalises the target discharge over the currently WET part of the patch, so
    # a patch that is only half submerged still delivers the full Q; this ceiling
    # stops that concentration from becoming a point sink if the wet area shrinks
    # to almost nothing. 1 mm/s is ~20x the rate a fully wet Isar patch needs.
    max_rate: float = 0.001
    # Also DRAIN standing surface water off the patch itself (fortran mode). The
    # patch is a porous gravel bar: water lying on top of it infiltrates rather than
    # ponding, so any surface water there is an artefact of the 2D model, which has
    # no subsurface to take it. With losing_region: line the prescribed exchange is
    # withdrawn from the channel at the losing line and nothing removes water that
    # stands on the bar - on isar-2025 that left 620 m2 of it perched above the
    # local water surface. The drain is an ADDITIONAL losing region covering the
    # patch (outside the strips already used by the prescribed exchange), depth-
    # limited by the same min_depth / taper_depth guard, and whatever it takes is
    # added to what the gaining line reinjects - so the routine stays mass-exact and
    # the prescribed exchange is never double-counted. Redundant (and skipped) when
    # losing_region is already 'patch'.
    patch_drain: bool = False
    # WATER TABLE under the porous patch. "phreatic" represents the bar's saturated
    # zone as the surface joining the two channel water levels the bar exchanges with
    # - the losing line upstream and the gaining line downstream - fitted as a plane
    # (that IS the water table of the through-flow, and its gradient is what drives
    # the exchange). It does two things:
    #   * the pre-wet seeds any ground below it, so closed depressions on the bar
    #     start full. They can never fill otherwise: a depression sitting ABOVE the
    #     channel cannot be reached by surface flow, and the drainable-seed filter
    #     deliberately refuses to seed it.
    #   * the patch drain tapers to zero at the table instead of at min_depth, so it
    #     clears standing water off the bar top but cannot empty a pool that cuts
    #     below the water table.
    # On isar-2025 the plane (817.318 m at the losing line -> 816.807 m at the
    # gaining line, gradient 7.18 permille, residual 0.054 m) wets 116 m2 / 10.4 m3
    # inside the patch - filling the 0.33 m deep pool to its rim - while leaving 95 %
    # of the patch dry with 0.37 m of freeboard. It is clipped to the patch polygon:
    # the same plane would sit above 22 598 m2 of bed elsewhere in the domain.
    water_table: str = "off"                    # off | phreatic (needs mode: fortran)
    # Explicit water level [m a.s.l.] per internal line, e.g.
    #   {losing: 817.318, gaining: 816.807}
    # Left unset, the levels are read off the seeded normal-depth surface at build
    # time, which on this case lands within +-0.07 m of the converged levels - and
    # with opposite signs at the two ends, so mid-patch (where the pool is) the two
    # agree to ~2 mm. Set it when the converged levels are known.
    water_table_levels: dict | None = None
    # [m/s] ceiling on the patch drain, in BOTH exchange modes. Defaults to max_rate,
    # which is loose enough not to bind. It matters because the Green-Ampt rate
    # applies over the whole WET patch, and the patch toe is genuinely wetted by the
    # channel: uncapped, the drain keeps drawing from that toe rather than only
    # clearing the water standing on the bar, and the total exchange reinjected at
    # the gaining line grows well past the calibrated one. Set it when the drain is
    # meant only to remove standing water (on isar-2025 the drain settled around
    # 0.05 m3/s against a ~0.10 m3/s calibrated exchange).
    patch_drain_max_rate: float | None = None

    @property
    def active(self) -> bool:
        """True when a gain-lose exchange is to be built.

        ``enabled`` is the new switch; the deprecated ``percolation:`` block said the
        same thing with ``mode`` other than ``off``, so both are honoured.
        """
        return bool(self.enabled) or self.mode != "off"

    @property
    def implementation(self) -> str:
        """``fortran`` | ``region`` - how the exchange is applied.

        ``fortran`` is the default for a newly enabled reach; ``mode`` only has to be
        set to choose ``region`` (or by an old config that spelled it out).
        """
        return "fortran" if self.mode == "off" else self.mode

    def validate(self) -> None:
        if self.mode not in ("off", "region", "fortran"):
            raise ValueError(
                f"gain_lose.mode must be 'off', 'region' or 'fortran', got {self.mode!r}"
            )
        if self.faces not in ("water-table", "lines"):
            raise ValueError("gain_lose.faces must be 'water-table' or 'lines', got "
                             f"{self.faces!r}")
        if self.losing_region not in ("patch", "line"):
            raise ValueError(
                "gain_lose.losing_region must be 'patch' or 'line', got "
                f"{self.losing_region!r}"
            )
        if not self.active:
            return
        if self.zone is None or not Path(self.zone).exists():
            raise FileNotFoundError(
                "gain_lose.enabled requires gain_lose.zone (the porous body, a "
                f"polygon layer); not found: {self.zone}"
            )
        if self.discharge is not None and self.discharge < 0:
            raise ValueError("gain_lose.discharge is the TOTAL exchange and must be "
                             f"positive (got {self.discharge})")
        if self.patch_drain and self.implementation != "fortran":
            raise ValueError(
                "gain_lose.patch_drain needs gain_lose.mode: fortran - the drain "
                "is a depth-limited withdrawal only the generated USER_RAIN routine "
                f"can apply (implementation is {self.implementation!r})"
            )
        if self.water_table not in ("off", "phreatic"):
            raise ValueError("gain_lose.water_table must be 'off' or 'phreatic', "
                             f"got {self.water_table!r}")
        if self.water_table != "off" and self.implementation != "fortran":
            raise ValueError(
                "gain_lose.water_table: phreatic needs gain_lose.mode: fortran - "
                "the table is the drain's taper floor inside the generated USER_RAIN "
                f"routine (implementation is {self.implementation!r})"
            )
        # last, because it is the least structural: with faces: lines the int-* lines'
        # own flow column can still supply the magnitude, so this only binds when the
        # faces come from the water table and there is nothing else to size them with
        if (self.faces == "water-table" and self.discharge is None
                and self.conductivity is None):
            raise ValueError(
                "gain_lose needs a magnitude: set `conductivity` (kf, the physical "
                "and calibratable route - see the reference in the config comment) "
                "or `discharge` (a measured total). With faces: lines the int-* "
                "lines' own flow column can supply it instead."
            )
        if self.water_table_levels is not None:
            unknown = set(self.water_table_levels) - {"losing", "gaining"}
            if unknown:
                raise ValueError(
                    "gain_lose.water_table_levels keys must be 'losing' and/or "
                    f"'gaining', got {sorted(unknown)}"
                )


#: Deprecated alias - the block was called ``Percolation`` while the feature lived in
#: the isar-2025 case. Kept so existing imports and case scripts keep working.
Percolation = GainLose


@dataclass
class Drying:
    """Removal of water too thin to be surface flow (a generated USER_RAIN term).

    On a coarse bed, water thinner than the grain roughness is not flowing over the
    bed - it is standing *within* it, and physically it drains into the substrate. A
    2D model has no substrate, so that water stays put and paints a wetted area far
    larger than the reach actually has: on isar-2025 the sub-centimetre band covered
    1503 m2 (13 % of the wetted area) while holding 4.4 m3 (0.16 % of the volume), at
    a mean speed of 0.000 m/s.

    TELEMAC offers no keyword for this. ``H CLIPPING`` + ``MINIMUM VALUE OF DEPTH``
    do the opposite - the dico states the truncation "is equivalent to adding mass" -
    and are marked "not fully implemented". So the withdrawal is generated into the
    same ``USER_RAIN`` routine the percolation exchange uses.

    The extracted water is **reinjected at the gaining line** together with the
    percolation water, which keeps the domain mass-exact. That matters for more than
    tidiness: a net domain sink S makes the steady-state boundary budget read
    ``|Q_in| - |Q_out| = S``, so a permanent sink would put a floor under the
    relative flux imbalance (0.0044 m3/s against 2.4 m3/s is 1.8e-3, above the 1e-3
    hydraulic tolerance) and no hotstart case would ever be written.
    """

    film_infiltration: bool = False
    # A node is treated as film when it is BOTH shallower than film_depth and slower
    # than film_velocity. The velocity gate is what keeps the term off the active
    # wet/dry margin, where the flow genuinely delivers water.
    film_depth: float = 0.01       # [m] below this depth the water may be film
    film_velocity: float = 0.005   # [m/s] ... and below this speed it is film
    # [m/s] withdrawal rate applied to film nodes, capped by the same
    # half-the-available-water-per-step limiter as the percolation sink, so it can
    # never dry a cell. 1e-5 m/s clears a 5 mm film in ~500 s.
    film_rate: float = 1.0e-5

    def validate(self, percolation=None) -> None:
        if not self.film_infiltration:
            return
        if self.film_depth <= 0 or self.film_velocity <= 0 or self.film_rate <= 0:
            raise ValueError(
                "drying.film_depth, film_velocity and film_rate must all be positive "
                "when drying.film_infiltration is on"
            )
        if percolation is not None and not (
                percolation.active and percolation.implementation == "fortran"):
            raise ValueError(
                "drying.film_infiltration currently needs a gain-lose reach in "
                "fortran mode. "
                "The term is generated into the same USER_RAIN routine, and the water "
                "it withdraws is reinjected at the percolation gaining line so the "
                "domain stays mass-exact - without a gaining line there is nowhere "
                "for it to go, and a net sink would put a floor under the boundary "
                f"flux imbalance (gain_lose is {percolation.implementation!r}, "
                f"active={percolation.active})"
            )


@dataclass
class GroundTruth:
    """Calibration ground truth: a tidy measurements table or raw sources to compile.

    Provide either ``measurements`` (a user-authored tidy multi-tab table) or
    ``sources`` (raw sources axqua compiles into that table). When ``sources``
    are given, ``measurements`` (if set) is the compiled artifact's output path.
    """

    measurements: Path | None = None     # tidy multi-tab ground-truth table (xlsx/csv)
    sources: list[GroundTruthSource] = field(default_factory=list)  # raw sources to compile
    targets: CalibrationTargets | None = None  # filled calibration-target-data.xlsx

    def problems(self) -> list[str]:
        """Return ground-truth input problems as messages (empty when all OK).

        Ground truth feeds only the *calibration / HydroBayesCal* setup (pipeline
        stage 5), never the TELEMAC model build itself, so a missing or mismatched
        source must NOT abort the build. Callers treat these as warnings: the model
        setup still completes and only the HydroBayesCal setup is skipped.
        """
        issues: list[str] = []
        # measurements is a user-authored tidy table only when no raw sources /
        # targets are given (otherwise it is the artifact compiled from them).
        if self.measurements is not None and not self.sources \
                and self.targets is None \
                and not Path(self.measurements).exists():
            issues.append(
                f"ground_truth.measurements set but not found: {self.measurements}"
            )
        for src in self.sources:
            for field_name in ("values", "positions"):
                p = getattr(src, field_name)
                if p is not None and not Path(p).exists():
                    issues.append(
                        f"ground_truth source '{src.category}' {field_name} not found: {p}"
                    )
        if self.targets is not None:
            for field_name in ("file", "hydraulics_positions", "sediment_positions"):
                p = getattr(self.targets, field_name)
                if p is not None and not Path(p).exists():
                    issues.append(
                        f"ground_truth.targets.{field_name} set but not found: {p}"
                    )
        return issues


@dataclass
class MeshConfig:
    """gmsh meshing controls.

    Two strategies, chosen automatically:

    * **Anisotropic, flow-aligned** (preferred) - when ``geodata.mesh_zones`` and
      ``geodata.channel_centerline`` are given. Each mesh-zone polygon is classified
      by its ``Zone Name`` (substring, case-insensitive) into one of three types and
      sized by its own ``Max Edge Length (m)`` field (``zone_size_field``; the
      ``*_size`` values below are only fallbacks when that field is absent/blank):
      ``channel`` zones get triangles elongated along the centerline (cross-channel
      edge = the zone's max edge length, stretched by ``channel_anisotropy`` along
      the flow); ``floodplain`` zones get near-equilateral triangles of the zone's
      edge length; ``refinement`` zones get near-equilateral triangles at their
      (typically smaller) edge length for **local refinement**. The size grows
      between zones at ``growth_ratio`` per element. Where zones overlap the finest
      intent wins (refinement > channel > floodplain). Implemented with a
      metric-tensor background mesh and gmsh's BAMG algorithm.
    * **Isotropic fallback** - ``default_size`` everywhere, refined to
      ``breakline_size`` along breaklines and per-MATID via ``region_sizes``.

    ``region_sizes`` maps a MATID (as int) to a target maximum element edge
    length (m); it mirrors the 'max_area' intent of region-pts-table.txt but in
    edge-length terms used by gmsh size fields.
    """

    # global multiplier on EVERY target edge length below, including the per-zone
    # `Max Edge Length (m)` values read from the mesh_zones gpkg (which otherwise
    # silently override the *_size fallbacks). The mesh-convergence study varies
    # this to coarsen/refine the whole sizing field uniformly.
    size_scale: float = 1.0

    # anisotropic, flow-aligned strategy (channel/floodplain zones + centerline)
    channel_size: float = 0.5           # channel edge length (m); fallback if not in the gpkg
    floodplain_size: float = 1.5        # floodplain edge length (m); also the default outside zones
    refinement_size: float = 0.5        # 'refinement' zone edge length (m); fallback if not in the gpkg
    growth_ratio: float = 1.2           # max edge-size growth per element (channel->floodplain)
    channel_anisotropy: float = 4.0     # along-flow / cross-flow edge ratio in the channel
    max_aspect_ratio: float = 4.0       # hard cap on cell aspect ratio (gmsh Mesh.AnisoMax)
    zone_name_field: str = "Zone Name"  # attribute in mesh_zones naming each zone
    # per-polygon max edge length [m] in mesh_zones (overrides the *_size defaults
    # above); read with a decimal-comma-tolerant parser so a German-locale gpkg works
    zone_size_field: str = "Max Edge Length (m)"
    roughness_zone_field: str = "Zone ID"  # integer-id attribute in roughness_zones

    # quality-report thresholds (warnings only; channel anisotropy is exempt)
    min_angle_deg: float = 30.0         # warn if floodplain cells dip below this
    max_angle_deg: float = 90.0         # warn on obtuse floodplain cells above this
    max_area_jump: float = 0.20         # warn on adjacent-cell area jumps over this (20%)

    # isotropic fallback strategy
    default_size: float = 10.0          # background target edge length (m)
    breakline_size: float = 3.0         # refined size along breaklines (m)
    region_sizes: dict[int, float] = field(default_factory=dict)
    min_size: float = 0.5
    grow_rate: float = 1.3              # gmsh Mesh.MeshSizeFromCurvature growth
    algorithm: int = 5                 # gmsh 2D algo (5 = Delaunay)


@dataclass
class FrictionZone:
    """A bed-friction zone tied to a MATID, the calibration target for TELEMAC."""

    matid: int
    name: str
    law: int = 4                       # 4 = Manning
    coefficient: float = 0.03          # initial value; perturbed by HydroBayesCal


@dataclass
class Friction:
    default_law: int = 4               # Manning
    default_coefficient: float = 0.03
    # law for the .tbl rows derived from geodata.roughness_zones + roughness_table
    # (5 = NIKU, Nikuradse k_s in metres - matching the ks roughness values)
    roughness_law: int = 5
    # lateral (wall) boundary friction -> TELEMAC LAW OF FRICTION ON LATERAL
    # BOUNDARIES / ROUGHNESS COEFFICIENT OF BOUNDARIES (3 = Strickler, 4 = Manning)
    boundary_law: int = 4
    boundary_coefficient: float = 0.03
    zones: list[FrictionZone] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Friction":
        zones = [FrictionZone(**_only_known(FrictionZone, z)) for z in d.get("zones", [])]
        return cls(
            default_law=d.get("default_law", 4),
            default_coefficient=d.get("default_coefficient", 0.03),
            roughness_law=d.get("roughness_law", 5),
            boundary_law=d.get("boundary_law", 4),
            boundary_coefficient=d.get("boundary_coefficient", 0.03),
            zones=zones,
        )


@dataclass
class Hydrodynamics:
    """Telemac2d steering knobs and prescribed boundary values."""

    regime: str = "steady"             # steady | unsteady
    # initial / maximum step. With variable_timestep on, TELEMAC shrinks it to hold
    # DESIRED COURANT NUMBER, so this is only the conservative starting step (0.25 s
    # keeps the first transient stable on a dry/wetting bed); the Courant target drives
    # the marching step thereafter.
    time_step: float = 0.25
    n_time_steps: int = 15000          # caps a FIXED-step run; for the variable-step run see duration
    # total simulated time [s] for the steady (variable-timestep) march. When None it
    # falls back to n_time_steps * time_step. Set it explicitly so the simulated
    # duration is decoupled from the (small) CFL start step above.
    duration: float | None = None
    graphic_printout_period: int = 500
    listing_printout_period: int = 500
    # OPT-IN steady-state auto-stop (TELEMAC's STOP IF A STEADY STATE IS REACHED).
    # OFF by default because it is UNRELIABLE with the variable time step: TELEMAC's
    # steady.f compares two CONSECUTIVE TIME STEPS with an ABSOLUTE per-step threshold
    # (|H - H_prev| < stop_criteria etc.), and the CFL-driven dt is tiny, so during a
    # slow transient (e.g. a low-discharge reach still filling from a dry start) the
    # per-step change drops below 1.E-4 m long before the boundary fluxes balance -
    # halting the run far from steady state. Convergence is judged instead by the
    # boundary-flux balance (axqua.flux_convergence). Enable this only with a
    # FIXED time step (variable_timestep: false), where a per-step change is meaningful.
    # stop_criteria order is H ; (U,V) ; tracers (TELEMAC's steady.f; the dico's English
    # help listing (U,V) first is wrong) - absolute changes in m, m/s, tracer units.
    stop_if_steady: bool = False
    stop_criteria: str = "1.E-4;1.E-4;1.E-4"   # absolute per-step change: H; (U,V); tracers
    # let TELEMAC adapt the time step to the CFL condition (DESIRED COURANT NUMBER):
    # a fixed time_step on a fine channel mesh (sub-metre cells) is a CFL violation
    # that diverges; with this on, time_step is only the initial/maximum step.
    # 0.6 is a conservative, stable target on the fine channel mesh (matching the 3D
    # run); raise it toward ~0.9 to march to steady state in fewer steps, or raise
    # n_time_steps if a run has not converged within the budget.
    variable_timestep: bool = True
    # 0.30 is a conservative, explosion-safe Courant target for the wetting/drying
    # steady march with k-epsilon on the fine channel mesh; raise it toward ~0.6-0.9
    # to reach steady state in fewer steps once a case is known to be stable.
    desired_courant: float = 0.30
    # discretisation: the classic finite-element kernel is the DEFAULT - it supports
    # the k-epsilon / Spalart-Allmaras / Smagorinski turbulence closures auto-selected
    # by turbulence_model below (the solver/* and tidal-flats keywords then apply).
    # Set finite_volumes=True for the HLLC finite-volume kernel instead - robust for
    # transcritical wetting/drying flow on steep terrain, but it accepts ONLY
    # turbulence model 1 (constant viscosity). finite_volume_scheme 5 = HLLC Riemann
    # solver, fv_space_order 2 = 2nd-order (MUSCL).
    finite_volumes: bool = False
    finite_volume_scheme: int = 5      # 0 Roe, 1 kinetic, 3 Zokagoa, 4 Tchamen, 5 HLLC, 6 WAF
    fv_space_order: int = 2
    # 0.9 strongly damps free-surface wiggles over steep bed gradients (FE; TELEMAC
    # default 1.0). Lower values (e.g. 0.1) under-damp and let the surface oscillate
    # into divergence on the high-aspect channel mesh.
    free_surface_gradient_compat: float = 0.9
    # CONTROL OF LIMITS + LIMIT VALUES: clip H/U/V/T so a local spike cannot cascade
    # to NaN (a divergence guard). Config-driven so switching it off for a diagnosis
    # is reproducible instead of a hand-edit to the generated .cas (lost on rebuild).
    control_of_limits: bool = True
    # Relative boundary-flux imbalance accepted as "converged" by the steady-run
    # analysis (axqua.flux_convergence). 1e-3 is the right grade when the result
    # is read as discharge / water depth / velocity - it is far inside field
    # measurement uncertainty and inside the mesh-convergence tolerance. Tighten to
    # 1e-4 when the result seeds a HydroBayesCal hotstart fleet, where a residual
    # transient would be inherited by every perturbed run.
    flux_tolerance: float = 1.0e-3
    # Depth [m] at or below which a node is NOT counted as wetted by the
    # wetted-extent report (axqua.wetting). This is a REPORTING convention, not a
    # model change: on a bed with Nikuradse ks 0.05-0.5 m, water 5 mm deep sits inside
    # the grain roughness and is not surface flow at all. On isar-2025 the sub-cm band
    # covered 1503 m2 - 13 % of the wetted AREA - while holding 4.4 m3, i.e. 0.16 % of
    # the volume: negligible in any budget, dominant in any picture. Use the same
    # threshold when filtering the result in ParaView so the picture and the report
    # agree. To actually remove that water from the model, see the `drying` block.
    wet_depth: float = 0.01
    # FE advection robustness for the distributive velocity scheme (14):
    #   implicitation 0.80 -> more implicit (stable) depth/velocity update (TELEMAC
    #     default 0.55, which is too explicit for the wetting front here);
    #   discretizations_in_space "11;11" -> LINEAR elements for H and U,V, required so
    #     the distributive scheme 14 is not rejected ("DISTRIBUTIVE SCHEMES NOT
    #     IMPLEMENTED FOR QUASI-BULLE AND QUADRATIC ELEMENTS");
    #   advection_sub_iterations / max_advection_iterations -> non-linearity sub-cycles
    #     and the per-step advection solve budget that keep the scheme converging.
    implicitation: float = 0.80
    discretizations_in_space: str = "11;11"
    advection_sub_iterations: int = 2       # NUMBER OF SUB-ITERATIONS FOR NON-LINEARITIES
    max_advection_iterations: int = 100     # MAXIMUM NUMBER OF ITERATIONS FOR ADVECTION SCHEMES
    # keep H unclipped (H CLIPPING : NO) so the tidal-flat / negative-depth treatment,
    # not a hard clip, handles drying - clipping H injects mass and destabilises the
    # wetting front. Set True only to hard-floor the depth as a last resort.
    h_clipping: bool = False
    # finite-element linear solver: 2 = preconditioned CG (robust where plain CG,
    # solver 1, stalls); preconditioning 2 = diagonal; solver_accuracy = tolerance.
    solver: int = 2
    preconditioning: int = 2
    solver_accuracy: float = 1.0e-5
    # turbulence-transport solver accuracy (ACCURACY OF K / EPSILON / SPALART-ALLMARAS):
    # TELEMAC's 1e-9 default is far tighter than the turbulence quantities need and is
    # unreachable within the 50-iteration cap while the dry-start domain is still
    # ill-conditioned - that is the transient "GRACJG: EXCEEDING MAXIMUM ITERATIONS 50"
    # warning. 1e-6 converges quickly (fewer iterations) with no loss of meaning.
    turbulence_solver_accuracy: float = 1.0e-6
    # solve budget for the k-epsilon step (MAXIMUM NUMBER OF ITERATIONS FOR K AND
    # EPSILON). TELEMAC's default 50 is too few while the dry-start domain is still
    # ill-conditioned; 120 lets the turbulence solve converge without relaxing accuracy.
    max_keps_iterations: int = 120
    # turbulence closure. "auto" (the default) picks among k-epsilon (3),
    # Spalart-Allmaras (6) and Smagorinski LES (4) from the channel mesh resolution
    # relative to the turbulence length scale (the flow depth) and the velocity guess
    # below, applying the 80%-TKE LES criterion - see steering.select_turbulence_model.
    # A literal int or name (1 const-visc, 3 k-epsilon, 4 Smagorinski, 6
    # Spalart-Allmaras) overrides the auto choice. NOTE: finite volumes accept ONLY
    # model 1; "auto"/3/4/6 require finite_volumes=False (the default kernel).
    turbulence_model: int | str = "auto"
    # mean-flow velocity guess [m/s] - the assumed initial average flow used to size
    # the turbulent eddy viscosity and feed the turbulence-model selection criteria.
    initial_velocity_guess: float = 1.0
    # turbulence integral length scale [m] for the resolution criterion; defaults to
    # the flow depth scale (prewet_depth, else 1.0 m) when left None.
    turbulence_length_scale: float | None = None
    velocity_diffusivity: float | None = None   # eddy viscosity [m2/s]; auto from U for model 1
    extra_keywords: dict[str, Any] = field(default_factory=dict)  # raw .cas overrides


@dataclass
class Morphodynamics:
    """GAIA morphodynamic block - extension point, off by default in v1.

    Two transport modes, independently toggleable and combinable (a GAIA run may do
    bedload only, suspension only, or both):

    * ``bedload`` -> ``BED LOAD FOR ALL SANDS : YES`` with
      ``BED-LOAD TRANSPORT FORMULA FOR ALL SANDS : bedload_formula`` in the GAIA
      steering file (1 = Meyer-Peter & Mueller).
    * ``suspended_load`` -> ``SUSPENSION FOR ALL SANDS : YES``; GAIA then transports
      the suspended sediment classes as TELEMAC **tracers** (no manual NUMBER OF
      TRACERS on the hydro side - GAIA declares them through the coupling).

    When ``enabled`` the hydrodynamic ``.cas`` gets ``COUPLING WITH : 'GAIA'`` +
    ``GAIA STEERING FILE`` and a ``COUPLING PERIOD FOR GAIA`` (hydro steps per GAIA
    step). The coupling is wired into **every** hydrodynamic run that can carry it -
    the steady ``steady2d.cas`` and, most usefully for morphodynamics, the
    hydrograph-driven ``unsteady2d.cas`` and its 3D twin ``unsteady3d.cas`` (a flood
    wave is what actually reworks the bed), so the same GAIA steering file drives 2D
    and 3D bed evolution.

    Beyond the two transport switches, the block exposes the bed-process capacities a
    real river morphodynamic run needs, all mapped to verified GAIA keywords:

    * ``morphological_factor`` -> ``MORPHOLOGICAL FACTOR`` - scales the bed evolution
      per hydraulic step so a long morphological response can be reached within a
      shorter hydrograph (1.0 = no acceleration).
    * ``slope_effect`` / ``slope_formula`` / ``friction_angle`` -> ``SLOPE EFFECT`` +
      ``FORMULA FOR SLOPE EFFECT`` (1 = Koch & Flokstra) + ``FRICTION ANGLE OF THE
      SEDIMENT`` [deg] - the gravitational pull that steers bedload down transverse
      bed slopes (bank erosion, bar shaping).
    * ``secondary_currents`` / ``secondary_currents_alpha`` -> ``SECONDARY CURRENTS``
      + ``SECONDARY CURRENTS ALPHA COEFFICIENT`` - the spiral-flow deviation of
      bedload in bends (relevant for a meandering reach).
    * ``bed_layers`` / ``active_layer_thickness`` -> ``NUMBER OF LAYERS FOR INITIAL
      STRATIFICATION`` + ``ACTIVE LAYER THICKNESS`` [m] - the bed stratigraphy for
      graded sediment / hiding.
    * ``prescribed_solid_discharges`` -> ``PRESCRIBED SOLID DISCHARGES`` [m3/s], one
      value per liquid boundary in the driver's FRONT2 order (0 = no supply /
      free-outflow; ``None`` = GAIA's equilibrium sediment inflow).
    * ``sediment_type`` -> ``CLASSES TYPE OF SEDIMENT`` (``NCO`` non-cohesive /
      ``CO`` cohesive), ``mass_balance`` -> ``MASS-BALANCE`` (log the sediment
      budget), ``graphic_printouts`` -> the GAIA ``VARIABLES FOR GRAPHIC PRINTOUTS``.
    """

    enabled: bool = False
    bedload: bool = True                 # BED LOAD FOR ALL SANDS : YES
    suspended_load: bool = False         # SUSPENSION FOR ALL SANDS : YES (carried as tracers)
    bedload_formula: int = 1             # BED-LOAD TRANSPORT FORMULA (1 = Meyer-Peter & Mueller)
    coupling_period: int = 1             # COUPLING PERIOD FOR GAIA (hydro steps per GAIA step)
    gaia_results_base: str = "rgaia"
    sediment_classes: list[dict[str, Any]] = field(default_factory=list)
    # --- bed-process capacities (all verified GAIA keywords; safe defaults) ---
    sediment_type: str = "NCO"           # CLASSES TYPE OF SEDIMENT (NCO non-cohesive / CO cohesive)
    morphological_factor: float = 1.0    # MORPHOLOGICAL FACTOR (bed-evolution acceleration)
    slope_effect: bool = True            # SLOPE EFFECT (transverse bed-slope pull on bedload)
    slope_formula: int = 1               # FORMULA FOR SLOPE EFFECT (1 = Koch & Flokstra)
    friction_angle: float = 40.0         # FRICTION ANGLE OF THE SEDIMENT [deg]
    secondary_currents: bool = False     # SECONDARY CURRENTS (spiral-flow bedload deviation in bends)
    secondary_currents_alpha: float = 1.0  # SECONDARY CURRENTS ALPHA COEFFICIENT
    bed_layers: int = 1                  # NUMBER OF LAYERS FOR INITIAL STRATIFICATION
    active_layer_thickness: float | None = None  # ACTIVE LAYER THICKNESS [m]; None -> GAIA default
    prescribed_solid_discharges: list[float] | None = None  # per liquid boundary (FRONT2 order)
    mass_balance: bool = True            # MASS-BALANCE (log the sediment budget)
    graphic_printouts: str | None = None  # GAIA VARIABLES FOR GRAPHIC PRINTOUTS; None -> default set
    extra_keywords: dict[str, Any] = field(default_factory=dict)


@dataclass
class DemOfDifference:
    """DEM-of-Difference (DoD) computation with a minimum level of detection (LoD).

    When ``enabled``, stage 1 computes the bed-change raster
    ``dem_target - dem_initial`` on the ROI-clipped initial grid (so the DoD is
    inherently clipped to ``geodata.boundary``) and applies a **minimum level of
    detection** (minLoD) - the survey-uncertainty floor below which a difference is
    indistinguishable from noise. Cells with ``|dz| < LoD`` are either masked to
    nodata (``mask_below_lod: true``, the default) or set to 0.

    The LoD [m] is resolved, in priority order, from:

    * an explicit ``min_lod`` (a fixed threshold you already trust), else
    * the **propagated survey uncertainty**
      ``LoD = t * sqrt(uncertainty_initial^2 + uncertainty_target^2)`` where each
      ``uncertainty_*`` is that DEM's vertical error (1-sigma, m) and ``t`` is the
      two-tailed critical value for ``confidence_level`` (0.95 -> 1.96), the
      best-available spatially-uniform propagated-error method (Brasington/Wheaton),
      else
    * 0 (report the raw difference, no thresholding) when neither is given.
    """

    enabled: bool = False
    uncertainty_initial: float | None = None   # vertical 1-sigma error of dem_initial [m]
    uncertainty_target: float | None = None    # vertical 1-sigma error of dem_target [m]
    confidence_level: float = 0.95             # two-tailed -> t/z critical value for the propagated LoD
    min_lod: float | None = None               # explicit minimum LoD [m]; overrides the propagated value
    mask_below_lod: bool = True                # |dz| < LoD -> nodata (True) or 0.0 (False)
    output: str = "dem-of-difference.tif"      # output filename in preprocessing_dir

    def validate(self) -> None:
        if not self.enabled:
            return
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError(
                "dem_of_difference.confidence_level must be in (0, 1), got "
                f"{self.confidence_level}"
            )
        for name in ("uncertainty_initial", "uncertainty_target", "min_lod"):
            v = getattr(self, name)
            if v is not None and v < 0.0:
                raise ValueError(f"dem_of_difference.{name} must be >= 0, got {v}")


@dataclass
class CalibrationParameter:
    """One calibration parameter forwarded to HydroBayesCal.

    ``name`` must follow HydroBayesCal's prefix convention:
    ``zone<N>`` (friction), ``gaia<KEYWORD> <class>`` (GAIA), ``vg_zone...``
    (vegetation), ``f.<name>`` (Fortran) or a literal TELEMAC keyword.
    """

    name: str
    min: float
    max: float
    comment: str = ""


@dataclass
class Calibration:
    parameters: list[CalibrationParameter] = field(default_factory=list)
    calibration_quantities: list[str] = field(default_factory=lambda: ["WATER DEPTH"])
    extraction_quantities: list[str] = field(
        default_factory=lambda: ["WATER DEPTH", "SCALAR VELOCITY"]
    )
    measurement_error: float = 0.10    # fraction of measured value
    # BAL / sampling settings (passed through to config_Telemac.py)
    init_runs: int = 30
    max_runs: int = 50
    parameter_sampling_method: str = "sobol"
    gp_library: str = "gpy"
    # HydroBayesCal predicts the surrogate at prior_samples points by building a
    # (prior_samples x prior_samples) dense covariance PER calibration location in
    # gpytorch; 25000 OOM-kills a 16 GB box. A low-dimensional roughness posterior
    # needs only a few thousand - 5000 is ample and memory-safe. Raise with RAM.
    prior_samples: int = 5000
    # Which solver HydroBayesCal drives, and which case files it perturbs. The
    # defaults calibrate the steady 2D case built by pipeline.run; a 3D profile
    # calibration points them at the 3D steering file instead (HydroBayesCal maps
    # 'Telemac3d' to telemac3d.py and extracts per-plane vertical profiles, picking
    # the sigma plane nearest each measurement height).
    solver_name: str = "Telemac2d"        # 'Telemac2d' | 'Telemac3d'
    control_file: str | None = None       # .cas to calibrate (default: cfg.cas_file)
    results_base: str | None = None       # results basename (default: from the .cas)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibration":
        params = [CalibrationParameter(**_only_known(CalibrationParameter, p))
                  for p in d.get("parameters", [])]
        rest = {k: v for k, v in d.items() if k != "parameters"}
        return cls(parameters=params, **_only_known(cls, rest))


@dataclass
class PreRun:
    """Run TELEMAC first, and seed the OpenFOAM case from its result.

    OpenFOAM is the expensive model in the chain and TELEMAC is cheap, so the water
    surface should be approximately known before ``interFoam`` starts. That seed does
    four jobs at once (see :mod:`axqua.solvers.openfoam.hotstart`): it puts the
    wetted cells of the 3D mesh into water at t=0, it clamps the lid a freeboard above
    the surface so most air cells never exist, it trims the plan footprint to the
    wetted corridor, and it gives every column a starting velocity.

    Without it a case is built cold under a flat lid - many times more air cells plus
    the whole filling transient, paid for in the slowest solver available.

    The seed is an approximation and is treated as one: the 3D free surface is still
    solved and free to leave it (that is what ``freeboard`` and ``wet_margin`` are
    room for, and :func:`axqua.solvers.openfoam.report.surface_freedom` checks
    afterwards whether it needed more). Under ``openfoam.mode: rigid-lid`` the surface
    is genuinely prescribed by the seed, which is the trade that mode exists to make.
    """

    # THE TOGGLE. False: use an existing r2d.slf if there happens to be one, never
    # start a solver. A user who has their own 2D result, or who wants the cold-start
    # behaviour, unchecks this.
    enabled: bool = True
    # A converged result from the real 2D study beats anything this block produces, so
    # it is used when present. False forces the dedicated coarse pre-run every time.
    reuse: bool = True
    dimension: str = "2d"           # 2d | 3d (3d also seeds the velocity profile)
    # Pin the sigma-plane count instead of inferring it from depth vs cell size.
    # For a reach whose vertical resolution has already been settled by
    # vertical_convergence_3d.py - and, deliberately, to override the inference on a
    # mesh it judges too flat, which is a choice worth making consciously rather than
    # by accident (see prerun.MIN_USEFUL_LEVELS).
    n_levels: int | None = None

    # ---- the dedicated coarse pre-run --------------------------------------
    # Multiplier on mesh.size_scale. This scales the WHOLE sizing field, including the
    # per-zone `Max Edge Length (m)` values carried in the mesh-zones gpkg - scaling
    # only channel_size/floodplain_size does nothing when the gpkg sets that field.
    size_scale: float = 2.0
    # The pre-run is pre-wetted, so it can march faster than the production dry start
    # (whose 0.30 default is sized for a wetting front advancing over a dry bed).
    courant: float = 0.6
    prewet_depth: float = 0.5       # [m] hotstart the channel mesh-zone
    duration: float | None = None   # [s] else hydrodynamics.duration
    n_processors: int | None = None  # else telemac.n_processors
    directory: str = "pre-run"      # under model_dir
    # What to do when the pre-run does not reach flux balance. A seed that is merely
    # close is still far better than a cold start, so `warn` is the default; `error`
    # is for a workflow that must not build on an unconverged surface.
    require: str = "warn"           # warn | error

    def validate(self) -> None:
        if self.dimension not in ("2d", "3d"):
            raise ValueError(
                f"openfoam.pre_run.dimension must be '2d' or '3d', "
                f"got {self.dimension!r}")
        if self.require not in ("warn", "error"):
            raise ValueError(
                f"openfoam.pre_run.require must be 'warn' or 'error', "
                f"got {self.require!r}")
        if self.size_scale <= 0:
            raise ValueError("openfoam.pre_run.size_scale must be > 0")
        if self.n_levels is not None and self.n_levels < 2:
            raise ValueError("openfoam.pre_run.n_levels must be at least 2 "
                             f"(sigma planes), got {self.n_levels}")


@dataclass
class OpenFoam:
    """The optional OpenFOAM free-surface (interFoam) extension.

    Entirely additive: a case config without an ``openfoam:`` block gets this
    dataclass at its defaults and nothing in the TELEMAC path ever consults it.
    See :mod:`axqua.solvers.openfoam` for what the values mean physically.

    The defaults are tuned for a small gravel-bed reach hotstarted from a converged
    TELEMAC 2D result. The two that matter most:

    * ``freeboard`` sets how much air the domain carries. The lid is clamped this far
      above the 2D free surface instead of being a flat plane over the terrain, which
      is what keeps the air phase from dominating the Courant number - but it must
      stay well above any surface rise the 3D run can produce, or the flow hits the
      lid.
    * ``auto_bed_layer`` pins the bed-adjacent layer to the reach's own ``ks`` so
      OpenFOAM's rough wall function is admissible. On a gravel bed that layer is a
      large fraction of the depth; refining past it silently invalidates the wall
      function rather than improving anything (see :mod:`axqua.solvers.openfoam.quality`).
    """

    # ---- environment --------------------------------------------------------
    # the OpenFOAM etc/bashrc to source (as telemac.pysource is for TELEMAC)
    bashrc: Path | None = None
    # how to enter that installation. On Windows this is `kind: wsl` plus a `distro`,
    # because the Foundation OpenFOAM axqua targets has no native Windows build.
    environment: Environment = field(default_factory=Environment)
    n_processors: int = 1
    solver: str = "interFoam"

    # ---- the TELEMAC pre-run that seeds this case ---------------------------
    pre_run: PreRun = field(default_factory=PreRun)

    # ---- what is actually solved --------------------------------------------
    # vof        two-phase interFoam: a resolved air-water interface. The surface can
    #            move, overtop a dam and wet a dry bar - and ~90% of the cells are air,
    #            which is what makes it slow.
    # rigid-lid  the lid sits ON the 2D free surface as a slip wall and the domain is
    #            water only. The air phase is removed by construction rather than
    #            approximated, so there is no interface, no air Courant limit and no
    #            air cells. The surface is then an INPUT (from the converged 2D run),
    #            not a result - which is the right trade when what you want is the
    #            vertical velocity structure, and the wrong one if you need the surface
    #            to respond. See axqua.solvers.openfoam.mesh.
    mode: str = "vof"

    # ---- mesh ---------------------------------------------------------------
    cell_size: float = 0.5          # plan lattice spacing dx [m]
    # Alternative to cell_size, expressed relative to the TELEMAC channel resolution:
    # 3.0 means "three times coarser than the 2D mesh". Self-adjusting across cases,
    # and the natural way to ask for a cheap test run.
    cell_size_factor: float | None = None
    n_layers: int = 14              # sigma layers per column
    domain: str = "wetted"          # wetted | roi
    wet_margin: float = 5.0         # [m] buffer around the 2D wetted extent
    lid: str = "follow"             # follow (the 2D free surface) | flat
    lid_elevation: float | None = None      # pin the lid to this level [m a.s.l.]
    freeboard: float = 0.5          # [m] air layer kept above the free surface
    # How freeboard / wet_margin are chosen. `auto` sizes both from the pre-run's own
    # flow (velocity head + a depth allowance; see State2D.headroom) and treats the
    # values above as FLOORS, so the surface keeps room to disagree with the 2D seed
    # on a fast reach without paying for air on a slow one. `fixed` uses them as
    # written. Ignored under mode: rigid-lid, where the lid IS the surface.
    headroom_mode: str = "auto"     # auto | fixed
    bank_slope: float = 0.1         # [-] used to turn a surface rise into wet_margin
    lid_smoothing: int = 2          # averaging passes over the lid
    # Running-max radius (in cells) applied before smoothing. 2, not 1: on the isar
    # reach a 1-cell dilation left one severely non-orthogonal face (70.16 deg, which
    # fails checkMesh) where the lid dipped over a shallow cell, and 2 removes it
    # while also improving the worst aspect ratio and the smallest cell volume.
    lid_dilation: int = 2
    align_to_flow: bool = True      # rotate the lattice onto the reach's axis
    min_column_height: float = 0.10  # [m] floor on lid - bed
    layer_expansion: float = 1.0    # top/bed layer thickness ratio
    bed_layer_height: float | None = None   # explicit bed-layer thickness [m]
    # Pin the bed layer to ~2x ks so the rough wall function is admissible. None means
    # "decide from the mode": on for the two-phase case, OFF under a rigid lid. Pinning
    # costs mesh quality - in a 0.13 m water column it consumes half the column and
    # leaves slivers above - and under a rigid lid on a shallow reach the wall function
    # is inadmissible at those depths anyway, so it would be paying for nothing.
    # Measured on isar at 5x coarsening: pinning gave 48 incorrectly oriented face
    # pyramids and 84 deg non-orthogonality; without it, 20 and 46 deg.
    auto_bed_layer: bool | None = None
    # rigid-lid only: the shallowest water column that is meshed at all, and the floor
    # on column height. A column thinner than this cannot be divided into n_layers
    # cells without collapsing into slivers that checkMesh rejects. 0.20 m took the
    # isar 5x test mesh to zero bad faces.
    min_water_depth: float = 0.20
    boundary_tolerance: float | None = None  # [m] liquid-line match (default 1.5*dx)
    max_plan_columns: int = 4_000_000

    # ---- physics ------------------------------------------------------------
    turbulence: str = "kOmegaSST"   # kOmegaSST | kEpsilon | laminar
    water_density: float = 998.2
    water_viscosity: float = 1.0e-6
    air_density: float = 1.0
    air_viscosity: float = 1.48e-5
    surface_tension: float = 0.07
    roughness_constant: float = 0.5   # nutkRoughWallFunction Cs
    friction_ks: float = 0.05         # fallback ks [m] with no roughness zones

    # ---- run ----------------------------------------------------------------
    spinup_time: float = 30.0       # [s] stage 1 (settle the interface)
    end_time: float = 300.0         # [s] end of stage 2
    write_interval: float = 10.0    # [s] of simulated time
    initial_time_step: float = 0.001
    max_time_step: float = 0.5
    max_courant: float = 0.9        # stage 2 (stage 1 uses spinup_courant)
    spinup_courant: float = 0.3
    # Cap on |U| enforced by the limitVelocity fvConstraint. Water never reaches it;
    # an air jet does, and this is the most effective single stop on the air phase
    # collapsing the time step. None -> 5x the expected water velocity.
    air_velocity_cap: float | None = None

    def validate(self) -> None:
        self.pre_run.validate()
        if self.mode not in ("vof", "rigid-lid"):
            raise ValueError(
                f"openfoam.mode must be 'vof' or 'rigid-lid', got {self.mode!r}")
        if self.cell_size_factor is not None and self.cell_size_factor <= 0:
            raise ValueError("openfoam.cell_size_factor must be > 0")
        if self.domain not in ("wetted", "roi"):
            raise ValueError(
                f"openfoam.domain must be 'wetted' or 'roi', got {self.domain!r}")
        if self.headroom_mode not in ("auto", "fixed"):
            raise ValueError("openfoam.headroom_mode must be 'auto' or 'fixed', "
                             f"got {self.headroom_mode!r}")
        if self.lid not in ("follow", "flat"):
            raise ValueError(f"openfoam.lid must be 'follow' or 'flat', got {self.lid!r}")
        if self.turbulence not in ("kOmegaSST", "kEpsilon", "laminar"):
            raise ValueError(
                "openfoam.turbulence must be 'kOmegaSST', 'kEpsilon' or 'laminar', "
                f"got {self.turbulence!r}")
        if self.cell_size <= 0:
            raise ValueError("openfoam.cell_size must be > 0")
        if self.n_layers < 1:
            raise ValueError("openfoam.n_layers must be >= 1")
        if self.freeboard <= 0:
            raise ValueError("openfoam.freeboard must be > 0 (the air layer above "
                             "the free surface)")
        if self.end_time <= self.spinup_time:
            raise ValueError(
                f"openfoam.end_time ({self.end_time}) must exceed spinup_time "
                f"({self.spinup_time}): stage 2 continues from where stage 1 stopped")
        if self.bashrc is not None and not Path(self.bashrc).is_file():
            raise FileNotFoundError(
                f"OpenFOAM bashrc not found: {self.bashrc}. Point openfoam.bashrc at "
                "the etc/bashrc of your install (e.g. "
                "/home/modelling/OpenFOAM/OpenFOAM-9/etc/bashrc)."
            )


# --------------------------------------------------------------------------- #
# top-level config
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    name: str
    crs_epsg: int
    config_dir: Path
    # all produced artifacts live under axqua-case/, split by workflow phase
    preprocessing_dir: Path   # preprocessing.py products (DEM clips, meshes, ground truth)
    model_dir: Path           # the TELEMAC case (build): geometry/cli/cas/tbl + results
    postprocessing_dir: Path  # mesh-convergence study and other post-processing
    calibration_dir: Path     # HydroBayesCal artifacts (calibration CSV, config_Telemac.py)
    telemac: TelemacEnv
    geodata: Geodata
    boundaries: Boundaries
    initialization: Initialization
    mesh: MeshConfig
    friction: Friction
    hydrodynamics: Hydrodynamics
    morphodynamics: Morphodynamics
    ground_truth: GroundTruth
    calibration: Calibration
    # DoD computation (optional; disabled by default so existing cases are unaffected)
    dem_of_difference: DemOfDifference = field(default_factory=DemOfDifference)
    # gain-lose reach: flow through a porous body (optional; off by default)
    gain_lose: GainLose = field(default_factory=GainLose)
    # removal of water too thin to be surface flow (optional; off by default)
    drying: Drying = field(default_factory=Drying)
    # dams / weirs / walls / buildings (optional; see axqua.core.structures)
    structures: Structures = field(default_factory=Structures)
    # OpenFOAM free-surface extension (optional; see axqua.solvers.openfoam)
    openfoam: OpenFoam = field(default_factory=OpenFoam)
    # where the OpenFOAM case tree is written; defaults to <sim_dir>/openfoam
    openfoam_dir: Path | None = None
    # Top-level blocks the YAML actually contained. Every solver section has a
    # dataclass with defaults, so `cfg.openfoam` exists whether or not the case asked
    # for OpenFOAM; this records what was *declared*, which is what decides whether a
    # solver is ENABLED for the case (see axqua.core.capabilities). Recorded as
    # raw key names rather than resolved backends so config.py stays free of any
    # knowledge of which solvers exist.
    declared_blocks: frozenset[str] = frozenset()

    # canonical output filenames inside model_dir -----------------------------
    geometry_slf: str = "geometry.slf"
    boundary_cli: str = "boundaries.cli"
    cas_file: str = "steady2d.cas"           # the initial run - always steady
    # additional hydrograph-driven run, written only when the inflow carries a
    # varying time series (otherwise a constant-Q inflow needs no unsteady case)
    unsteady_cas_file: str = "unsteady2d.cas"
    liquid_boundaries_file: str = "inflow-hydrograph.liq"  # Q(t)/SL(t) for unsteady2d.cas
    # serialized FRONT2-ordered LiquidBoundary list written during the build, so the
    # standalone unsteady / 3D scripts get the boundary numbering without re-meshing
    liquid_boundaries_json: str = "liquid-boundaries.json"
    control_sections_file: str = "control-sections.txt"    # unsteady flux-verification sections
    sections_output_file: str = "r-control-sections.txt"   # SECTIONS OUTPUT FILE
    results_unsteady_slf: str = "r2d-unsteady.slf"         # unsteady2d.cas RESULTS FILE
    results3d_unsteady_slf: str = "r3d-unsteady.slf"       # unsteady telemac3d 3D RESULT FILE
    results2d_from_3d_unsteady_slf: str = "r3d-2d-unsteady.slf"  # unsteady 3D 2D RESULT FILE
    ic_slf: str = "initial-conditions.slf"   # hotstart file when prewet_depth is set
    # internal source/sink polygons (SOURCE REGIONS DATA FILE) for a losing-gaining reach
    source_regions_file: str = "source-regions.txt"
    user_fortran_dir: str = "user_fortran"   # FORTRAN FILE folder (percolation.mode: fortran)
    friction_tbl: str = "friction.tbl"
    zones_file: str = "zones.bfr"
    gaia_cas: str = "gaia.cas"
    results_slf: str = "r2d.slf"
    results3d_slf: str = "r3d.slf"          # TELEMAC-3D 3D RESULT FILE (add3d.py)
    results2d_from_3d_slf: str = "r3d-2d.slf"  # TELEMAC-3D depth-averaged 2D RESULT FILE
    ground_truth_xlsx: str = "ground-truth.xlsx"  # compiled tidy table (in preprocessing_dir)
    calibration_csv: str = "measurements-calibration.csv"
    hbc_config: str = "config_Telemac.py"
    # per-phase compound logfile name; each script logs into its own output folder
    log_file: str = "axqua.log"

    def preprocessing_path(self, filename: str) -> Path:
        return Path(self.preprocessing_dir) / filename

    def model_path(self, filename: str) -> Path:
        return Path(self.model_dir) / filename

    def postprocessing_path(self, filename: str) -> Path:
        return Path(self.postprocessing_dir) / filename

    def calibration_path(self, filename: str) -> Path:
        return Path(self.calibration_dir) / filename

    @property
    def rigid_lid(self) -> bool:
        """Convenience: is the OpenFOAM case single-phase under a fixed lid?"""
        return self.openfoam.mode == "rigid-lid"

    @property
    def openfoam_case_dir(self) -> Path:
        """The OpenFOAM case root (``<sim_dir>/openfoam`` unless configured).

        Derived rather than required so a config predating the OpenFOAM extension
        still resolves it; it sits beside ``simulation/``, not inside it, because an
        OpenFOAM case owns its whole directory (``0/ constant/ system/ processor*/``).
        """
        if self.openfoam_dir is not None:
            return Path(self.openfoam_dir)
        return Path(self.model_dir).parent / "openfoam"

    def openfoam_path(self, filename: str) -> Path:
        return self.openfoam_case_dir / filename

    @property
    def ground_truth_path(self) -> Path:
        """Where the tidy ground-truth table lives (explicit input, else compiled)."""
        if self.ground_truth.measurements is not None:
            return Path(self.ground_truth.measurements)
        return self.preprocessing_path(self.ground_truth_xlsx)

    def validate(self) -> None:
        self.telemac.validate()
        self.geodata.validate()
        self.boundaries.validate()
        # ground truth is non-fatal: it feeds only the calibration/HydroBayesCal
        # setup, so a missing/mismatched source warns and skips that setup rather
        # than aborting the model build (see GroundTruth.problems / pipeline stage 5).
        for problem in self.ground_truth.problems():
            log.warning("%s; HydroBayesCal setup will be skipped", problem)
        cond = self.boundaries.outflow_condition
        if cond == "stage_discharge" and self.boundaries.stage_discharge is None:
            raise ValueError(
                "outflow_condition: stage_discharge requires boundaries.stage_discharge "
                "(a Q-h rating CSV alongside the config). Generate one from a "
                "Manning/Strickler value and channel geometry with `axqua rating`."
            )
        if cond == "elevation" and self.boundaries.prescribed_elevation is None:
            raise ValueError(
                "outflow_condition: elevation requires boundaries.prescribed_elevation"
            )
        if self.initialization.prewet_depth is not None and self.geodata.mesh_zones is None:
            raise ValueError(
                "initialization.prewet_depth needs geodata.mesh_zones (with a "
                "'*channel*' Zone Name) to define the region to pre-wet."
            )
        if self.morphodynamics.enabled and self.geodata.dem_target is None \
                and self.geodata.dem_of_difference is None:
            raise ValueError(
                "morphodynamics.enabled but no geodata.dem_target / dem_of_difference "
                "provided for topographic-change calibration data."
            )
        self.percolation.validate()
        self.telemac.environment.validate()
        self.openfoam.environment.validate()
        self.structures.validate()
        self.openfoam.validate()
        self.initialization.validate()
        self.drying.validate(self.percolation)
        self.dem_of_difference.validate()
        if self.dem_of_difference.enabled and self.geodata.dem_target is None \
                and self.geodata.dem_of_difference is None:
            raise ValueError(
                "dem_of_difference.enabled but no geodata.dem_target (to difference "
                "against geodata.dem_initial) nor a precomputed geodata.dem_of_difference."
            )

    @property
    def percolation(self) -> GainLose:
        """Deprecated alias for :attr:`gain_lose`.

        ``percolation`` was the name while the feature lived in the isar-2025 case;
        it is now core axqua and is spelled ``gain_lose``. Kept so existing case
        scripts and configs keep working.
        """
        return self.gain_lose

    def ensure_dirs(self) -> None:
        for d in (self.preprocessing_dir, self.model_dir,
                  self.postprocessing_dir, self.calibration_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


def _load_environment(raw, cfg_dir: Path) -> "Environment":
    """Build an :class:`Environment` from a config block.

    A bare string is accepted as shorthand for the kind (``environment: wsl``), since
    that is the common case and a one-key mapping would be noise.
    """
    if raw is None:
        return Environment()
    if isinstance(raw, str):
        return Environment(kind=raw)
    data = _only_known(Environment, dict(raw))
    # A WSL setup script is a path INSIDE the distro, so it must not be resolved
    # against the config directory on the host.
    if data.get("setup_script") is not None and data.get("kind") != "wsl":
        data["setup_script"] = _resolve(cfg_dir, data["setup_script"])
    return Environment(**data)


def _load_gain_lose(raw: dict, cfg_dir: Path) -> GainLose:
    """Build the :class:`GainLose` block from ``gain_lose:`` or the deprecated
    ``percolation:``.

    The feature was called *percolation* while it lived in the isar-2025 case; it is
    now core and spelled ``gain_lose``. The old block still loads, mapped onto the
    new fields:

    * ``mode: off`` meant "no exchange", which is now ``enabled: false``; any other
      mode means enabled;
    * ``losing_region: line`` pinned the exchange to the ``int-*`` lines, which is
      now ``faces: lines`` (``patch`` mapped to the zone, i.e. ``water-table``);
    * everything else keeps its name.

    A config carrying both blocks is a mistake worth surfacing rather than resolving
    silently.
    """
    new = dict(raw.get("gain_lose") or {})
    old = dict(raw.get("percolation") or {})
    if new and old:
        raise ValueError(
            "config has both `gain_lose:` and the deprecated `percolation:` block - "
            "keep only `gain_lose:` (see the GainLose docstring for the mapping)"
        )
    if old:
        log.warning("`percolation:` is deprecated - rename the block to `gain_lose:` "
                    "(mode -> enabled, losing_region -> faces); loading it as before")
        new = old
        if "mode" in new:
            new.setdefault("enabled", str(new["mode"]).lower() != "off")
        else:
            new.setdefault("enabled", True)
        if "losing_region" in new:
            new.setdefault(
                "faces", "lines" if new["losing_region"] == "line" else "water-table")
        else:
            new.setdefault("faces", "lines")   # the old default was the int-* lines
    if "zone" in new and new["zone"] is not None:
        new["zone"] = _resolve(cfg_dir, new["zone"])
    return GainLose(**_only_known(GainLose, new))


def load_config(path: str | os.PathLike) -> Config:
    """Load and validate a YAML config into a :class:`Config`."""
    path = Path(path).resolve()
    cfg_dir = path.parent
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    # deprecated keys, renamed in place and reported once (axqua.core.schema)
    schema.warn_legacy(schema.apply_legacy(raw), path.name)

    project = raw.get("project", {})
    sim_dir = _resolve_sim_dir(cfg_dir, project.get("sim_dir"))
    # per-phase output dirs under axqua-case/
    preprocessing_dir = _resolve(
        cfg_dir, project.get("preprocessing_dir") or f"{sim_dir}/preprocessing")
    model_dir = _resolve(cfg_dir, project.get("model_dir") or f"{sim_dir}/simulation")
    postprocessing_dir = _resolve(
        cfg_dir, project.get("postprocessing_dir") or f"{sim_dir}/postprocessing")
    calibration_dir = _resolve(
        cfg_dir, project.get("calibration_dir")
        or f"{sim_dir}/calibration-validation")

    # telemac env
    tdict = dict(raw.get("telemac", {}))
    tdict["pysource"] = _resolve(cfg_dir, tdict.get("pysource"))
    tdict["environment"] = _load_environment(tdict.get("environment"), cfg_dir)
    telemac = TelemacEnv(**_only_known(TelemacEnv, tdict))

    # geodata (resolve every path against the config dir)
    gdict = dict(raw.get("geodata") or {})
    _geodata_scalars = {"control_section_name_field"}   # plain strings, not paths
    for key in list(gdict):
        if key not in _geodata_scalars:
            gdict[key] = _resolve(cfg_dir, gdict[key])
    geodata = Geodata(**_only_known(Geodata, gdict))

    # boundaries: liquid-boundary lines / inflow / rating are paths; the rest scalars
    bdict = dict(raw.get("boundaries") or {})
    for key in ("liquid_boundaries", "inflow", "stage_discharge"):
        if key in bdict:
            bdict[key] = _resolve(cfg_dir, bdict[key])
    boundaries = Boundaries(**_only_known(Boundaries, bdict))

    initialization = Initialization(
        **_only_known(Initialization, raw.get("initialization", {}) or {}))

    # ground truth: a compiled/authored measurements table + raw sources to compile
    gtdict = dict(raw.get("ground_truth") or {})
    gt_raw = gtdict.pop("sources", None) or []
    measurements = _resolve(cfg_dir, gtdict.get("measurements"))
    sources = []
    for src in gt_raw:
        src = _only_known(GroundTruthSource, src)
        src["values"] = _resolve(cfg_dir, src.get("values"))
        src["positions"] = _resolve(cfg_dir, src.get("positions"))
        sources.append(GroundTruthSource(**src))
    targets = None
    tdict = gtdict.get("targets")
    if tdict:
        tdict = _only_known(CalibrationTargets, dict(tdict))
        for key in ("file", "hydraulics_positions", "sediment_positions"):
            if key in tdict:
                tdict[key] = _resolve(cfg_dir, tdict[key])
        targets = CalibrationTargets(**tdict)
    ground_truth = GroundTruth(measurements=measurements, sources=sources,
                               targets=targets)

    mesh = MeshConfig(**_only_known(MeshConfig, raw.get("mesh", {}) or {}))
    # YAML maps keys may come back as str; coerce region_sizes keys to int
    mesh.region_sizes = {int(k): float(v) for k, v in (mesh.region_sizes or {}).items()}

    friction = Friction.from_dict(raw.get("friction", {}) or {})
    hydro = Hydrodynamics(**_only_known(Hydrodynamics, raw.get("hydrodynamics", {}) or {}))
    morph = Morphodynamics(**_only_known(Morphodynamics, raw.get("morphodynamics", {}) or {}))
    dod = DemOfDifference(
        **_only_known(DemOfDifference, raw.get("dem_of_difference", {}) or {}))
    gain_lose = _load_gain_lose(raw, cfg_dir)
    drying = Drying(**_only_known(Drying, raw.get("drying", {}) or {}))
    structures = Structures(**_only_known(Structures, raw.get("structures", {}) or {}))
    calib = Calibration.from_dict(raw.get("calibration", {}) or {})

    # OpenFOAM extension: absent block -> defaults, and nothing consults them
    ofdict = dict(raw.get("openfoam") or {})
    if "bashrc" in ofdict and ofdict["bashrc"] is not None:
        ofdict["bashrc"] = _resolve(cfg_dir, ofdict["bashrc"])
    ofdict["environment"] = _load_environment(ofdict.get("environment"), cfg_dir)
    ofdict["pre_run"] = PreRun(**_only_known(PreRun, dict(ofdict.get("pre_run") or {})))
    openfoam = OpenFoam(**_only_known(OpenFoam, ofdict))
    openfoam_dir = _resolve(cfg_dir, project.get("openfoam_dir")) if \
        project.get("openfoam_dir") else None

    cfg = Config(
        name=project.get("name", path.stem),
        crs_epsg=int(project.get("crs_epsg", 25832)),
        config_dir=cfg_dir,
        preprocessing_dir=preprocessing_dir,
        model_dir=model_dir,
        postprocessing_dir=postprocessing_dir,
        calibration_dir=calibration_dir,
        telemac=telemac,
        geodata=geodata,
        boundaries=boundaries,
        initialization=initialization,
        mesh=mesh,
        friction=friction,
        hydrodynamics=hydro,
        morphodynamics=morph,
        ground_truth=ground_truth,
        calibration=calib,
        dem_of_difference=dod,
        gain_lose=gain_lose,
        drying=drying,
        structures=structures,
        openfoam=openfoam,
        openfoam_dir=openfoam_dir,
        declared_blocks=frozenset(raw),
    )
    # apply optional output-filename overrides
    for key, value in (raw.get("outputs", {}) or {}).items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


# --------------------------------------------------------------------------- #
# writing a config back out
# --------------------------------------------------------------------------- #

# Blocks whose contents are pure derived state or machine-local bookkeeping, and so
# have no place in a config file that someone else may open.
_DUMP_SKIP_TOP = frozenset({"config_dir", "declared_blocks"})


def _dump_value(value: Any, base: Path) -> Any:
    """Convert one config value into something YAML can round-trip.

    Paths are written **relative to the config file** wherever they sit underneath it,
    exactly as ``load_config`` resolves them - so a dumped config stays portable to
    another machine, which is the whole reason a case ships its data under its own
    directory. A path outside that tree (a solver install, an absolute scratch dir)
    is written absolute, because making it relative would be a lie about where it is.
    """
    if isinstance(value, Path):
        try:
            return str(value.relative_to(base))
        except ValueError:
            return str(value)
    if isinstance(value, (list, tuple)):
        return [_dump_value(v, base) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_dump_value(v, base) for v in value)
    if isinstance(value, dict):
        return {str(k): _dump_value(v, base) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _dump_value(getattr(value, f.name), base)
                for f in fields(value)}
    return value


def config_to_dict(cfg: "Config", *, base: Path | None = None) -> dict[str, Any]:
    """The config as plain data, shaped the way ``load_config`` reads it back."""
    base = Path(base) if base else Path(cfg.config_dir)
    out: dict[str, Any] = {"project": {
        "name": cfg.name,
        "crs_epsg": cfg.crs_epsg,
        "preprocessing_dir": _dump_value(cfg.preprocessing_dir, base),
        "model_dir": _dump_value(cfg.model_dir, base),
        "postprocessing_dir": _dump_value(cfg.postprocessing_dir, base),
        "calibration_dir": _dump_value(cfg.calibration_dir, base),
    }}
    if cfg.openfoam_dir is not None:
        out["project"]["openfoam_dir"] = _dump_value(cfg.openfoam_dir, base)

    for f in fields(cfg):
        if f.name in _DUMP_SKIP_TOP or f.name in out["project"]:
            continue
        value = getattr(cfg, f.name)
        if hasattr(value, "__dataclass_fields__"):
            out[f.name] = _dump_value(value, base)

    # output filename overrides, only where they differ from the defaults - a dump
    # should record decisions, not restate the defaults back at the reader
    defaults = {"geometry_slf": "geometry.slf", "boundary_cli": "boundaries.cli",
                "cas_file": "steady2d.cas", "results_slf": "r2d.slf"}
    outputs = {k: getattr(cfg, k) for k, v in defaults.items()
               if hasattr(cfg, k) and getattr(cfg, k) != v}
    if outputs:
        out["outputs"] = outputs
    return out


def dump_config(cfg: "Config", path: str | os.PathLike | None = None, *,
                base: Path | None = None) -> str:
    """Write *cfg* back out as YAML; returns the text, and writes it if given a path.

    The counterpart to :func:`load_config`, and the reason it exists is that anything
    which *edits* a case - a form in a plugin, a migration, a scripted sweep - needs
    to put the result back. Doing that by rewriting the YAML text keeps the comments
    but requires a parser that understands the file; going through the dataclass loses
    the comments but cannot produce a config that will not load.

    This takes the second route deliberately. A comment that survives an edit it no
    longer describes is worse than no comment, and the template configs in
    ``cases/case-template/`` are where the explanations are meant to live.
    """
    text = yaml.safe_dump(config_to_dict(cfg, base=base), sort_keys=False,
                          default_flow_style=False, allow_unicode=True)
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        log.info("wrote %s", path)
    return text
