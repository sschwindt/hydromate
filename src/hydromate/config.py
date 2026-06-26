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

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


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
class TelemacEnv:
    """How to reach and run the TELEMAC solver.

    ``pysource`` is the shell script that, when sourced, exports HOMETEL and the
    TELEMAC PYTHONPATH (e.g. ``configs/pysource.mint22.sh``). The pipeline does
    not import TELEMAC's Python directly; it sources this script in a subshell
    for any solver call (see :mod:`hydromate.env`).
    """

    pysource: Path
    solver: str = "telemac2d"  # telemac2d | telemac3d
    n_processors: int = 1
    version: str = "v8p4"

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
class Inputs:
    """User-provided geodata and hydraulics (paths resolved against the config dir)."""

    dem_initial: Path                    # baseline terrain (GeoTIFF)
    boundary: Path                       # ROI / max wetted extent polygon (shp/gpkg)
    liquid_boundaries: Path              # inflow/outflow boundary lines (shp/gpkg)
    inflow: Path                         # discharge time series / value (csv)
    dem_target: Path | None = None       # optional 2nd DEM for morphodynamic target
    breaklines: Path | None = None       # internal constraint lines (shp/gpkg)
    mesh_zones: Path | None = None       # polygons with a 'Zone Name' (channel/floodplain) for sizing
    channel_centerline: Path | None = None  # line the channel cells are elongated along
    roughness_zones: Path | None = None  # polygons with a 'Zone ID' for per-node roughness
    roughness_table: Path | None = None  # csv: zone_id, roughness (e.g. ks) -> calibrated by HBC
    region_points: Path | None = None    # seed points carrying MATID + max area
    region_table: Path | None = None     # region-pts-table.txt (MATID legend)
    stage_discharge: Path | None = None  # outlet rating curve (csv), optional
    measurements: Path | None = None     # tidy multi-tab ground-truth table (xlsx/csv)
    ground_truth: list[GroundTruthSource] = field(default_factory=list)  # raw sources to compile
    dem_of_difference: Path | None = None  # precomputed DoD (else derived from DEMs)

    def validate(self) -> None:
        required = {
            "dem_initial": self.dem_initial,
            "boundary": self.boundary,
            "liquid_boundaries": self.liquid_boundaries,
            "inflow": self.inflow,
        }
        missing = [name for name, p in required.items() if p is None or not Path(p).exists()]
        if missing:
            raise FileNotFoundError(f"Missing required inputs: {missing}")
        for name in ("dem_target", "breaklines", "mesh_zones", "channel_centerline",
                     "roughness_zones", "roughness_table", "region_points",
                     "region_table", "stage_discharge", "dem_of_difference"):
            p = getattr(self, name)
            if p is not None and not Path(p).exists():
                raise FileNotFoundError(f"inputs.{name} set but not found: {p}")
        # measurements is a user-authored tidy table only when no raw ground_truth
        # sources are given (otherwise it is the artifact compiled from them).
        if self.measurements is not None and not self.ground_truth \
                and not Path(self.measurements).exists():
            raise FileNotFoundError(f"inputs.measurements set but not found: {self.measurements}")
        for src in self.ground_truth:
            for field_name in ("values", "positions"):
                p = getattr(src, field_name)
                if p is not None and not Path(p).exists():
                    raise FileNotFoundError(
                        f"ground_truth source '{src.category}' {field_name} not found: {p}"
                    )


@dataclass
class MeshConfig:
    """gmsh meshing controls.

    Two strategies, chosen automatically:

    * **Anisotropic, flow-aligned** (preferred) - when ``inputs.mesh_zones`` and
      ``inputs.channel_centerline`` are given. Each mesh-zone polygon is classified
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
    # law for the .tbl rows derived from inputs.roughness_zones + roughness_table
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
    time_step: float = 1.0
    n_time_steps: int = 15000
    graphic_printout_period: int = 500
    listing_printout_period: int = 500
    # let TELEMAC adapt the time step to the CFL condition (DESIRED COURANT NUMBER):
    # a fixed time_step on a fine channel mesh (sub-metre cells) is a CFL violation
    # that diverges; with this on, time_step is only the initial/maximum step.
    variable_timestep: bool = True
    desired_courant: float = 0.9
    # discretisation: finite volumes (default) are robust for transcritical, wetting/
    # drying flow on steep terrain; finite_volume_scheme 5 = HLLC Riemann solver,
    # fv_space_order 2 = 2nd-order (MUSCL). Set finite_volumes=False for the classic
    # finite-element kernel (then the solver/* and tidal-flats keywords below apply).
    finite_volumes: bool = True
    finite_volume_scheme: int = 5      # 0 Roe, 1 kinetic, 3 Zokagoa, 4 Tchamen, 5 HLLC, 6 WAF
    fv_space_order: int = 2
    free_surface_gradient_compat: float = 0.1   # damps free-surface wiggles (FE; default 1.0)
    # finite-element linear solver: 2 = preconditioned CG (robust where plain CG,
    # solver 1, stalls); preconditioning 2 = diagonal; solver_accuracy = tolerance.
    solver: int = 2
    preconditioning: int = 2
    solver_accuracy: float = 1.0e-5
    # turbulence closure. NOTE: finite volumes accept ONLY model 1 (constant
    # viscosity); k-epsilon (3) and Spalart-Allmaras (6) require finite_volumes=False.
    turbulence_model: int = 1          # 1 const-visc, 3 k-epsilon, 6 Spalart-Allmaras
    velocity_diffusivity: float | None = None   # constant eddy viscosity [m2/s] for model 1
    # downstream (outflow) boundary type:
    #   "stage_discharge" -> prescribed water level (cli 5 4 4) read from the
    #                        inputs.stage_discharge rating curve at the simulated
    #                        Q (the default; one Q-h pair suffices for steady runs)
    #   "elevation"       -> prescribed water level (cli 5 4 4) from prescribed_elevation
    #   "free"            -> Neumann / free outflow (cli 4 4 4), nothing prescribed
    outflow_condition: str = "stage_discharge"
    prescribed_flowrate: float | None = None   # upstream Q (m3/s); else from inflow
    prescribed_elevation: float | None = None  # downstream WSE (m); only for outflow_condition=elevation
    initial_conditions: str = "ZERO DEPTH"
    # pre-wetting: when set, write a warm-start SELAFIN seeding this water depth
    # (m) on the channel mesh-zone nodes (dry elsewhere) and continue the run from
    # it, so wetting need not advance from a dry bed (speeds up steady runs)
    prewet_depth: float | None = None
    extra_keywords: dict[str, Any] = field(default_factory=dict)  # raw .cas overrides


@dataclass
class Morphodynamics:
    """GAIA morphodynamic block - extension point, off by default in v1."""

    enabled: bool = False
    gaia_results_base: str = "rgaia"
    sediment_classes: list[dict[str, Any]] = field(default_factory=list)
    extra_keywords: dict[str, Any] = field(default_factory=dict)


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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibration":
        params = [CalibrationParameter(**_only_known(CalibrationParameter, p))
                  for p in d.get("parameters", [])]
        rest = {k: v for k, v in d.items() if k != "parameters"}
        return cls(parameters=params, **_only_known(cls, rest))


# --------------------------------------------------------------------------- #
# top-level config
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    name: str
    crs_epsg: int
    config_dir: Path
    # all produced artifacts live under tm-simulation/, split by workflow phase
    preprocessing_dir: Path   # preprocessing.py products (DEM clips, meshes, ground truth)
    model_dir: Path           # the TELEMAC case (build): geometry/cli/cas/tbl + results
    postprocessing_dir: Path  # mesh-convergence study and other post-processing
    calibration_dir: Path     # HydroBayesCal artifacts (calibration CSV, config_Telemac.py)
    telemac: TelemacEnv
    inputs: Inputs
    mesh: MeshConfig
    friction: Friction
    hydrodynamics: Hydrodynamics
    morphodynamics: Morphodynamics
    calibration: Calibration

    # canonical output filenames inside model_dir -----------------------------
    geometry_slf: str = "geometry.slf"
    boundary_cli: str = "boundaries.cli"
    cas_file: str = "steady2d.cas"
    ic_slf: str = "initial-conditions.slf"   # warm-start file when prewet_depth is set
    friction_tbl: str = "friction.tbl"
    zones_file: str = "zones.bfr"
    gaia_cas: str = "gaia.cas"
    results_slf: str = "r2d.slf"
    ground_truth_xlsx: str = "ground-truth.xlsx"  # compiled tidy table (in preprocessing_dir)
    calibration_csv: str = "measurements-calibration.csv"
    hbc_config: str = "config_Telemac.py"
    # per-phase compound logfile name; each script logs into its own output folder
    log_file: str = "hydromate.log"

    def preprocessing_path(self, filename: str) -> Path:
        return Path(self.preprocessing_dir) / filename

    def model_path(self, filename: str) -> Path:
        return Path(self.model_dir) / filename

    def postprocessing_path(self, filename: str) -> Path:
        return Path(self.postprocessing_dir) / filename

    def calibration_path(self, filename: str) -> Path:
        return Path(self.calibration_dir) / filename

    @property
    def ground_truth_path(self) -> Path:
        """Where the tidy ground-truth table lives (explicit input, else compiled)."""
        if self.inputs.measurements is not None:
            return Path(self.inputs.measurements)
        return self.preprocessing_path(self.ground_truth_xlsx)

    def validate(self) -> None:
        self.telemac.validate()
        self.inputs.validate()
        cond = self.hydrodynamics.outflow_condition
        if cond not in ("stage_discharge", "elevation", "free"):
            raise ValueError(
                "hydrodynamics.outflow_condition must be 'stage_discharge', "
                f"'elevation', or 'free', got {cond!r}"
            )
        if cond == "stage_discharge" and self.inputs.stage_discharge is None:
            raise ValueError(
                "outflow_condition: stage_discharge requires inputs.stage_discharge "
                "(a Q-h rating CSV alongside the config). Generate one from a "
                "Manning/Strickler value and channel geometry with `hydromate rating`."
            )
        if cond == "elevation" and self.hydrodynamics.prescribed_elevation is None:
            raise ValueError(
                "outflow_condition: elevation requires hydrodynamics.prescribed_elevation"
            )
        if self.hydrodynamics.prewet_depth is not None and self.inputs.mesh_zones is None:
            raise ValueError(
                "hydrodynamics.prewet_depth needs inputs.mesh_zones (with a "
                "'*channel*' Zone Name) to define the region to pre-wet."
            )
        if self.morphodynamics.enabled and self.inputs.dem_target is None \
                and self.inputs.dem_of_difference is None:
            raise ValueError(
                "morphodynamics.enabled but no inputs.dem_target / dem_of_difference "
                "provided for topographic-change calibration data."
            )

    def ensure_dirs(self) -> None:
        for d in (self.preprocessing_dir, self.model_dir,
                  self.postprocessing_dir, self.calibration_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


def load_config(path: str | os.PathLike) -> Config:
    """Load and validate a YAML config into a :class:`Config`."""
    path = Path(path).resolve()
    cfg_dir = path.parent
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    project = raw.get("project", {})
    sim_dir = project.get("sim_dir", "tm-simulation")
    # per-phase output dirs under tm-simulation/ (older work_dir/results_dir accepted)
    preprocessing_dir = _resolve(
        cfg_dir, project.get("preprocessing_dir") or project.get("work_dir")
        or f"{sim_dir}/preprocessing")
    model_dir = _resolve(cfg_dir, project.get("model_dir") or f"{sim_dir}/simulation")
    postprocessing_dir = _resolve(
        cfg_dir, project.get("postprocessing_dir") or f"{sim_dir}/postprocessing")
    calibration_dir = _resolve(
        cfg_dir, project.get("calibration_dir") or project.get("results_dir")
        or f"{sim_dir}/calibration-validation")

    # telemac env
    tdict = dict(raw.get("telemac", {}))
    tdict["pysource"] = _resolve(cfg_dir, tdict.get("pysource"))
    telemac = TelemacEnv(**_only_known(TelemacEnv, tdict))

    # inputs (resolve every path against the config dir)
    idict = dict(raw.get("inputs", {}))
    gt_raw = idict.pop("ground_truth", None) or []
    for key in list(idict):
        idict[key] = _resolve(cfg_dir, idict[key])
    ground_truth = []
    for src in gt_raw:
        src = _only_known(GroundTruthSource, src)
        src["values"] = _resolve(cfg_dir, src.get("values"))
        src["positions"] = _resolve(cfg_dir, src.get("positions"))
        ground_truth.append(GroundTruthSource(**src))
    inputs = Inputs(**_only_known(Inputs, idict), ground_truth=ground_truth)

    mesh = MeshConfig(**_only_known(MeshConfig, raw.get("mesh", {}) or {}))
    # YAML maps keys may come back as str; coerce region_sizes keys to int
    mesh.region_sizes = {int(k): float(v) for k, v in (mesh.region_sizes or {}).items()}

    friction = Friction.from_dict(raw.get("friction", {}) or {})
    hydro = Hydrodynamics(**_only_known(Hydrodynamics, raw.get("hydrodynamics", {}) or {}))
    morph = Morphodynamics(**_only_known(Morphodynamics, raw.get("morphodynamics", {}) or {}))
    calib = Calibration.from_dict(raw.get("calibration", {}) or {})

    cfg = Config(
        name=project.get("name", path.stem),
        crs_epsg=int(project.get("crs_epsg", 25832)),
        config_dir=cfg_dir,
        preprocessing_dir=preprocessing_dir,
        model_dir=model_dir,
        postprocessing_dir=postprocessing_dir,
        calibration_dir=calibration_dir,
        telemac=telemac,
        inputs=inputs,
        mesh=mesh,
        friction=friction,
        hydrodynamics=hydro,
        morphodynamics=morph,
        calibration=calib,
    )
    # apply optional output-filename overrides
    for key, value in (raw.get("outputs", {}) or {}).items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
