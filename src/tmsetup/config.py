"""User configuration framework for the TELEMAC setup pipeline.

A single YAML file (see ``config/inn.yml``) describes the whole case: where the
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
    for any solver call (see :mod:`tmsetup.env`).
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
class Inputs:
    """User-provided geodata and hydraulics (paths resolved against the config dir)."""

    dem_initial: Path                    # baseline terrain (GeoTIFF)
    boundary: Path                       # ROI / max wetted extent polygon (shp/gpkg)
    liquid_boundaries: Path              # inflow/outflow boundary lines (shp/gpkg)
    inflow: Path                         # discharge time series / value (csv)
    dem_target: Path | None = None       # optional 2nd DEM for morphodynamic target
    breaklines: Path | None = None       # internal constraint lines (shp/gpkg)
    region_points: Path | None = None    # seed points carrying MATID + max area
    region_table: Path | None = None     # region-pts-table.txt (MATID legend)
    stage_discharge: Path | None = None  # outlet rating curve (csv), optional
    measurements: Path | None = None     # hydraulic calibration points (csv/shp)
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
        for name in ("dem_target", "breaklines", "region_points", "region_table",
                     "stage_discharge", "measurements", "dem_of_difference"):
            p = getattr(self, name)
            if p is not None and not Path(p).exists():
                raise FileNotFoundError(f"inputs.{name} set but not found: {p}")


@dataclass
class MeshConfig:
    """gmsh meshing controls.

    ``region_sizes`` maps a MATID (as int) to a target maximum element edge
    length (m); it mirrors the 'max_area' intent of region-pts-table.txt but in
    edge-length terms used by gmsh size fields.
    """

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
    zones: list[FrictionZone] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Friction":
        zones = [FrictionZone(**_only_known(FrictionZone, z)) for z in d.get("zones", [])]
        return cls(
            default_law=d.get("default_law", 4),
            default_coefficient=d.get("default_coefficient", 0.03),
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
    turbulence_model: int = 3          # 3 = k-epsilon
    prescribed_flowrate: float | None = None   # upstream Q (m3/s); else from inflow
    prescribed_elevation: float | None = None  # downstream WSE (m); else from stage_discharge
    initial_conditions: str = "ZERO DEPTH"
    extra_keywords: dict[str, Any] = field(default_factory=dict)  # raw .cas overrides


@dataclass
class Morphodynamics:
    """GAIA morphodynamic block — extension point, off by default in v1."""

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
    work_dir: Path
    model_dir: Path
    results_dir: Path
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
    friction_tbl: str = "friction.tbl"
    zones_file: str = "zones.bfr"
    gaia_cas: str = "gaia.cas"
    results_slf: str = "r2d.slf"
    calibration_csv: str = "measurements-calibration.csv"
    hbc_config: str = "config_Telemac.py"

    def validate(self) -> None:
        self.telemac.validate()
        self.inputs.validate()
        if self.morphodynamics.enabled and self.inputs.dem_target is None \
                and self.inputs.dem_of_difference is None:
            raise ValueError(
                "morphodynamics.enabled but no inputs.dem_target / dem_of_difference "
                "provided for topographic-change calibration data."
            )

    def ensure_dirs(self) -> None:
        for d in (self.work_dir, self.model_dir, self.results_dir):
            Path(d).mkdir(parents=True, exist_ok=True)

    # convenience absolute paths ---------------------------------------------
    def model_path(self, filename: str) -> Path:
        return Path(self.model_dir) / filename


def load_config(path: str | os.PathLike) -> Config:
    """Load and validate a YAML config into a :class:`Config`."""
    path = Path(path).resolve()
    cfg_dir = path.parent
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    project = raw.get("project", {})
    work_dir = _resolve(cfg_dir, project.get("work_dir", "case")) or (cfg_dir / "case")
    model_dir = _resolve(cfg_dir, project.get("model_dir")) or (work_dir / "simulation")
    results_dir = _resolve(cfg_dir, project.get("results_dir")) or (work_dir / "results")

    # telemac env
    tdict = dict(raw.get("telemac", {}))
    tdict["pysource"] = _resolve(cfg_dir, tdict.get("pysource"))
    telemac = TelemacEnv(**_only_known(TelemacEnv, tdict))

    # inputs (resolve every path against the config dir)
    idict = dict(raw.get("inputs", {}))
    for key in list(idict):
        idict[key] = _resolve(cfg_dir, idict[key])
    inputs = Inputs(**_only_known(Inputs, idict))

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
        work_dir=work_dir,
        model_dir=model_dir,
        results_dir=results_dir,
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
