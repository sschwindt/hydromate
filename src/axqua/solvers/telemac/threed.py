"""Extrude the calibrated 2D case into a non-hydrostatic TELEMAC-3D run.

``add3d.build_3d_cas`` turns the converged 2D steady result (the ``initial_run``
output ``r2d.slf``) into a ``<case-name>3d.cas`` for ``telemac3d.py``:

* **Hotstart from the 2D run (TELEMAC v9+)** - by default ``FILE FOR 2D CONTINUATION``
  reads the final 2D state and initialises the 3D free surface + horizontal velocities
  from it (vertical velocity 0; horizontal velocity copied up each column). In v9 this
  keyword alone triggers the continuation (the old ``2D CONTINUATION`` flag is gone).
  If a thin/supercritical 2D inflow makes ``telemac3d`` abort with ``DEBIMP_3D`` on the
  inflow boundary, swap in the commented-out cold-start ``CONSTANT DEPTH`` seed (median
  wetted 2D depth) - the ``hotstart="constant_depth"`` mode the vertical convergence
  study uses, which over-wets the inflow so the prescribed-Q boundary stays subcritical.
  ``INITIAL GUESS FOR DEPTH`` reuses the previous depth and a ``MINIMAL VALUE FOR
  DEPTH`` floor guards the sigma transform against dry (zero-depth) columns.
* **Vertical discretisation** - TELEMAC has **no Delft3D-style constant-dz Z-layer**
  option; the vertical mesh is **sigma planes** (``NUMBER OF HORIZONTAL LEVELS`` +
  ``MESH TRANSFORMATION : 1``), so each water column is split into equal sigma
  slices and the physical ``dz`` is proportional to the local depth. We therefore
  *infer the level count* from the 2D results so a representative ``dz`` lands near
  the horizontal cell size: ``dz ~ dx/2`` (:data:`TARGET_ASPECT`) while never
  letting ``dz`` get more than 4x finer than ``dx`` (:data:`MAX_ASPECT`, the user's
  "max 4 times less" rule), clamped to a sane level count for a workable 3D mesh.
* **Turbulence** - the same quality-checked selection as 2D
  (:func:`axqua.steering.select_turbulence_model`), mapped onto the TELEMAC-3D
  numbering (k-epsilon 3, Smagorinski 4, Spalart-Allmaras **5** - note 3D numbers
  S-A 5, not the 2D 6). A vertical closure is paired with it (mixing length, or
  k-epsilon when the horizontal model is k-epsilon).
* **Non-hydrostatic** - ``NON-HYDROSTATIC VERSION : YES`` (with the pressure-Poisson
  solver), since a depth-resolved river model wants the non-hydrostatic pressure.
  ``non_hydrostatic=False`` writes the cheaper hydrostatic solver instead (no PPE
  keywords) - used for the long steady flux-convergence check ``add3d.py`` emits.
* **Courant** - TELEMAC-3D has **no** ``DESIRED COURANT NUMBER`` / variable time
  step, so we size the fixed ``TIME STEP`` to hit a target Courant of
  :data:`TARGET_COURANT` (0.6) from the 2D velocity and the cell sizes.

TELEMAC-3D cannot run with finite volumes, so the 3D case is always the
finite-element kernel (the 2D ``finite_volumes`` flag is irrelevant here). Zonal
friction is not carried over - TELEMAC-3D has no ``FRICTION DATA FILE`` - so a
single representative bottom-friction law/coefficient is written instead.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from axqua.core import selafin
from axqua.config import Config
from axqua.solvers.telemac.steering import TURB_NAMES, _global_friction, select_turbulence_model

log = logging.getLogger("axqua")

# 2D turbulence-model number -> TELEMAC-3D HORIZONTAL TURBULENCE MODEL number.
# Note: Spalart-Allmaras is 5 in 3D (it is 6 in 2D); constant/Elder fall back to
# k-epsilon so the 3D run always carries a real turbulence closure.
_TURB_2D_TO_3D = {1: 3, 2: 3, 3: 3, 4: 4, 6: 5}
TURB3D_NAMES = {1: "constant viscosity", 2: "mixing length", 3: "k-epsilon",
                4: "Smagorinski", 5: "Spalart-Allmaras", 7: "k-omega"}

N_LEVELS_MIN = 5         # a workable 3D water column needs a handful of planes
N_LEVELS_MAX = 21        # cap the vertical resolution (cost / stiffness)
TARGET_ASPECT = 2.0      # desired horizontal:vertical cell ratio dx/dz
MAX_ASPECT = 4.0         # dz must stay >= dx / MAX_ASPECT (the "max 4x finer" rule)
TARGET_COURANT = 0.6     # target advective Courant number for the fixed 3D time step
WET = 0.05               # [m] depth above which a node counts as wetted

# the three steering files add3d.py emits (see build_3d_cases)
HYDROSTATIC_CAS = "hotstart3d_hydrostatic.cas"
# The hydrostatic run's own results. Named rather than inlined because the seed
# orchestration (axqua.prerun) has to find them without re-deriving the string.
HYDROSTATIC_RESULT_3D = "r3d-hydrostatic.slf"
HYDROSTATIC_RESULT_2D = "r3d-2d-hydrostatic.slf"
HYDRODYN_CAS = "hotstart3d_hydrodyn.cas"
UNSTEADY3D_CAS = "unsteady3d.cas"
HYDROSTATIC_N_STEPS = 30_000       # fixed step count of the flux-convergence run
HYDROSTATIC_LISTING_PERIOD = 100   # listing spacing -> ~300 flux printouts


@dataclass
class VerticalDiscretization:
    n_levels: int        # NUMBER OF HORIZONTAL LEVELS (sigma planes)
    dz: float            # representative layer thickness [m] at the typical depth
    dx: float            # representative horizontal cell size [m]
    depth: float         # representative (median wetted) flow depth [m]
    aspect: float        # dx / dz at that depth
    reason: str


@dataclass
class ThreeDSetup:
    cas: Path
    n_levels: int
    dz: float
    dx: float
    depth: float
    time_step: float
    n_time_steps: int
    h_turbulence: int
    v_turbulence: int
    courant: float
    turbulence_reason: str
    layers_reason: str
    non_hydrostatic: bool = True


# --------------------------------------------------------------------------- #
# reading the 2D result
# --------------------------------------------------------------------------- #

def _fetch(data: dict, *needles: str) -> np.ndarray | None:
    """Return the first result variable whose (lower-cased) name contains a needle."""
    for name in data["var_names"]:
        low = name.lower()
        if any(n in low for n in needles):
            return data["values"][name]
    return None


def _depth(data: dict) -> np.ndarray | None:
    depth = _fetch(data, "water depth")
    if depth is None:
        fs, bot = _fetch(data, "free surface"), _fetch(data, "bottom")
        if fs is not None and bot is not None:
            depth = fs - bot
    return depth


def _median_edge(tri: np.ndarray, x: np.ndarray, y: np.ndarray) -> float:
    p = np.column_stack([x, y])
    e = np.concatenate([
        np.linalg.norm(p[tri[:, 0]] - p[tri[:, 1]], axis=1),
        np.linalg.norm(p[tri[:, 1]] - p[tri[:, 2]], axis=1),
        np.linalg.norm(p[tri[:, 2]] - p[tri[:, 0]], axis=1),
    ])
    return float(np.median(e)) if e.size else 0.0


def representative_scales(data: dict) -> tuple[float, float]:
    """(median wetted depth h, representative wetted horizontal cell size dx) [m]."""
    depth = _depth(data)
    tri, x, y = data["ikle"], data["x"], data["y"]
    wet = depth > WET if depth is not None else None
    h = float(np.median(depth[wet])) if (wet is not None and wet.any()) else 1.0
    if wet is not None and wet.any():
        sel = wet[tri].all(axis=1)            # triangles fully in the wetted area
        if sel.any():
            tri = tri[sel]
    dx = _median_edge(tri, x, y) or 1.0
    return h, dx


def characteristic_velocity(cfg: Config, data: dict) -> float:
    """A high-percentile wetted flow speed [m/s] (for the Courant time step),
    floored at the configured velocity guess."""
    sv = _fetch(data, "scalar velocity")
    if sv is None:
        u, v = _fetch(data, "velocity u"), _fetch(data, "velocity v")
        sv = np.hypot(u, v) if (u is not None and v is not None) else None
    floor = abs(float(cfg.hydrodynamics.initial_velocity_guess)) or 1.0
    if sv is None:
        return floor
    depth = _depth(data)
    wet = depth > WET if depth is not None else None
    vals = sv[wet] if (wet is not None and wet.any()) else sv
    return max(float(np.percentile(vals, 95)), floor)


# --------------------------------------------------------------------------- #
# inference
# --------------------------------------------------------------------------- #

def infer_vertical_layers(cfg: Config, data: dict) -> VerticalDiscretization:
    """Infer ``NUMBER OF HORIZONTAL LEVELS`` from the 2D results.

    The sigma layer thickness at the representative depth ``h`` is ``dz = h/(n-1)``.
    We aim for ``dz ~ dx/2`` (TARGET_ASPECT) but never thinner than ``dx/4``
    (MAX_ASPECT - the user's "max 4x finer than dx/dy" rule), and keep the level
    count within ``[N_LEVELS_MIN, N_LEVELS_MAX]`` so the 3D mesh stays workable.
    """
    h, dx = representative_scales(data)
    # most levels allowed before dz drops below dx/MAX_ASPECT
    n_aspect_cap = max(2, int(math.floor(h / (dx / MAX_ASPECT))) + 1)
    n_target = int(round(h / (dx / TARGET_ASPECT))) + 1
    n = int(np.clip(n_target, N_LEVELS_MIN, N_LEVELS_MAX))
    n = max(2, min(n, n_aspect_cap))
    dz = h / (n - 1)
    aspect = dx / dz
    note = ""
    if n < N_LEVELS_MIN:
        note = (f"; aspect cap limits to {n} (<{N_LEVELS_MIN}) - cells are flat "
                f"(dx={dx:.2f} m >> h={h:.2f} m)")
    reason = (f"depth h~{h:.2f} m, dx~{dx:.2f} m -> {n} sigma levels "
              f"(dz~{dz:.2f} m, dx/dz~{aspect:.1f} <= {MAX_ASPECT:.0f}){note}")
    return VerticalDiscretization(n, dz, dx, h, aspect, reason)


def select_3d_turbulence(cfg: Config, mesh) -> tuple[int, int, str]:
    """(horizontal model, vertical model, rationale) for TELEMAC-3D.

    Reuses the 2D mesh-resolution/velocity selection and maps it onto the 3D
    numbering; pairs a vertical closure (k-epsilon when horizontal is k-epsilon,
    else a mixing-length model, which is robust for the vertical log profile)."""
    base, why = select_turbulence_model(cfg, mesh)
    h3d = _TURB_2D_TO_3D.get(base, 3)
    # TELEMAC-3D constrains the vertical model: Spalart-Allmaras (5) must be used in
    # BOTH directions; k-epsilon (3) pairs with k-epsilon; Smagorinski (4) pairs with
    # a vertical mixing-length model (2), the robust choice for the vertical profile.
    v3d = {3: 3, 5: 5}.get(h3d, 2)
    reason = f"{TURB3D_NAMES[h3d]} (from 2D {TURB_NAMES.get(base, base)}) - {why}"
    return h3d, v3d, reason


def courant_time_step(dx: float, dz: float, u_char: float,
                      courant: float = TARGET_COURANT) -> float:
    """Fixed time step [s] giving an advective Courant ~ *courant* on the limiting
    (smallest) cell dimension. TELEMAC-3D has no DESIRED COURANT NUMBER keyword, so
    this is how the requested Courant is honoured."""
    length = max(min(dx, dz), 1e-6)
    dt = courant * length / max(u_char, 1e-6)
    if dt <= 0:
        return 0.1
    # round to 2 significant figures for a tidy steering value
    ndigits = -int(math.floor(math.log10(dt))) + 1
    return round(dt, max(ndigits, 1))


# --------------------------------------------------------------------------- #
# writing the 3D steering
# --------------------------------------------------------------------------- #

def _read_cas_values(path: Path, *keywords: str) -> dict[str, str | None]:
    """Values of *keywords* from a steering file (``KEY : value`` or ``KEY = value``),
    ignoring comment lines - one file read/pass for all keywords."""
    pats = {kw: re.compile(rf"^\s*{re.escape(kw)}\s*[:=]\s*(.+?)\s*$", re.IGNORECASE)
            for kw in keywords}
    found: dict[str, str | None] = dict.fromkeys(keywords)
    for line in Path(path).read_text().splitlines():
        if line.lstrip().startswith(("/", "*")):
            continue
        for kw, pat in pats.items():
            if found[kw] is None:
                m = pat.match(line)
                if m:
                    found[kw] = m.group(1).strip()
    return found


def cas3d_name(cfg: Config, suffix: str = "") -> str:
    """``<case-name>3d<suffix>.cas`` with a filesystem-safe case name.

    *suffix* distinguishes variants written next to each other (e.g.
    ``"-unsteady"`` -> ``<case-name>3d-unsteady.cas``)."""
    slug = "".join(c if (c.isalnum() or c in "-_") else "_" for c in cfg.name)
    return f"{(slug.strip('_') or 'case')}3d{suffix}.cas"


def _force_levels(vd: VerticalDiscretization, n_levels: int) -> VerticalDiscretization:
    """A VerticalDiscretization with a forced level count (e.g. for the vertical
    convergence study), recomputing dz/aspect from the same depth and cell size."""
    n = max(2, int(n_levels))
    dz = vd.depth / (n - 1)
    return VerticalDiscretization(
        n, dz, vd.dx, vd.depth, vd.dx / dz,
        f"forced to {n} sigma levels (dz~{dz:.2f} m, dx/dz~{vd.dx / dz:.1f})")


def build_3d_cas(cfg: Config, results_2d: str | Path | None = None,
                 cas_2d: str | Path | None = None,
                 total_time_factor: float = 4.0,
                 n_levels: int | None = None,
                 hotstart: str = "continuation",
                 unsteady: bool = False,
                 liquid_boundaries_file: str | None = None,
                 duration: float | None = None,
                 gaia_cas: str | None = None,
                 out_name: str | None = None,
                 results3d_name: str | None = None,
                 results2d_name: str | None = None,
                 non_hydrostatic: bool = True,
                 n_time_steps: int | None = None,
                 listing_period: int | None = None,
                 graphic_period: int | None = None,
                 min_depth: float | None = None,
                 data: dict | None = None) -> ThreeDSetup:
    """Write ``<case-name>3d.cas`` next to the 2D case and return the setup summary.

    *results_2d* defaults to ``<model_dir>/<results_slf>`` (the ``initial_run``
    output); *cas_2d* to the steady 2D steering, whose prescribed boundary values
    are reused. *total_time_factor* sets the simulated duration as that many reach
    flow-through times (length / characteristic velocity). *n_levels* overrides the
    inferred ``NUMBER OF HORIZONTAL LEVELS`` (used by the vertical convergence study;
    the time step then adapts to the resulting layer thickness).

    With *unsteady* and a *liquid_boundaries_file* the run is hydrograph-driven: the
    ``LIQUID BOUNDARIES FILE`` supplies Q(t)/SL(t) and the step count spans *duration*
    (TELEMAC-3D has no DURATION keyword, so ``NUMBER OF TIME STEPS = duration / dt``);
    the hotstart still lifts the initial 3D field from the 2D result. *gaia_cas*
    couples GAIA morphodynamics. *out_name* / *results3d_name* / *results2d_name*
    override the output filenames so an unsteady variant does not clobber the steady
    3D run's files.

    *non_hydrostatic* toggles ``NON-HYDROSTATIC VERSION`` (``False`` writes the
    hydrostatic solver and drops the pressure-Poisson keywords - the cheap variant
    for a long boundary-flux convergence run). *n_time_steps* forces the step count
    (overriding the flow-through/duration sizing); *listing_period* /
    *graphic_period* override the printout spacing (e.g. a short listing period so
    the flux-convergence analysis gets enough printouts). *data* is an optional
    pre-read 2D result (``selafin.read_slf``) so several variants can be built from
    one read of a large results file.

    *hotstart* selects how the 3D run is initialised from the 2D result:

    * ``"continuation"`` (default) - the TELEMAC v9+ true 2D->3D hotstart
      (``FILE FOR 2D CONTINUATION``): the 3D free surface + horizontal velocities are
      lifted from the final 2D state. The most faithful start, but a thin/supercritical
      2D inflow can make ``telemac3d`` abort with ``DEBIMP_3D: PROBLEM ON BOUNDARY``;
      the ``CONSTANT DEPTH`` fallback is then emitted commented-out to swap in.
    * ``"constant_depth"`` - a cold start at the median wetted 2D depth (uniform), so
      the prescribed-Q inflow is deeply wet and robust. Used by the vertical
      convergence study, where every per-layer run must reach steady state.
    """
    model = Path(cfg.model_dir)
    results_2d = Path(results_2d) if results_2d else model / cfg.results_slf
    cas_2d = Path(cas_2d) if cas_2d else model / cfg.cas_file
    if not results_2d.exists():
        raise FileNotFoundError(
            f"2D result not found: {results_2d}. Run initial_run.py first so the 3D "
            "case can hotstart from the converged 2D field."
        )

    if data is None:
        data = selafin.read_slf(results_2d)
    vd = infer_vertical_layers(cfg, data)
    if n_levels is not None:
        vd = _force_levels(vd, n_levels)
    from types import SimpleNamespace
    mesh = SimpleNamespace(x=data["x"], y=data["y"], triangles=data["ikle"])
    h_turb, v_turb, turb_reason = select_3d_turbulence(cfg, mesh)
    u_char = characteristic_velocity(cfg, data)
    dt = courant_time_step(vd.dx, vd.dz, u_char)

    # duration: for an unsteady run, span the supplied hydrograph *duration* (3D has
    # no DURATION keyword, so convert to a step count at the fixed dt); otherwise a
    # few reach flow-through times so the 3D field settles from the 2D hotstart.
    reach_len = float(max(np.ptp(data["x"]), np.ptp(data["y"]))) or (u_char * dt * 1000)
    if n_time_steps is not None:
        n_steps = max(1, int(n_time_steps))
    else:
        if unsteady and duration is not None:
            total_time = float(duration)
        else:
            total_time = total_time_factor * reach_len / max(u_char, 1e-6)
        n_steps = int(np.clip(round(total_time / dt), 200, 200_000))
    # 3D result writing is expensive; keep only a handful of frames over the spin-up
    graphic = graphic_period or max(1, n_steps // 5)
    listing = listing_period or max(1, n_steps // 10)

    law, coef = _global_friction(cfg)
    # positive depth floor (~5% of a layer) so dry hotstart columns don't divide by 0;
    # callers can raise it (e.g. 0.10 m for the KB15 continuation runs, which need a
    # firmer floor to keep the sigma transform / PPE stable on the dry floodplain).
    depth_floor = (float(min_depth) if min_depth is not None
                   else round(max(0.005, 0.05 * vd.dz), 4))
    # reuse the 2D prescriptions + horizontal velocity profiles verbatim (boundary
    # consistency); one pass over the 2D steering for all three keywords
    vals = (_read_cas_values(cas_2d, "PRESCRIBED FLOWRATES", "PRESCRIBED ELEVATIONS",
                             "VELOCITY PROFILES")
            if cas_2d.exists() else {})
    flow = vals.get("PRESCRIBED FLOWRATES")
    elev = vals.get("PRESCRIBED ELEVATIONS")
    prof = vals.get("VELOCITY PROFILES")
    n_liquid = (len(prof.split(";")) if prof else
                (len(flow.split(";")) if flow else 0))

    if hotstart == "constant_depth":
        hotstart_lines = [
            "/ HOTSTART (cold start): seed a uniform depth = the median wetted 2D depth,",
            "/ so the prescribed-Q inflow starts deeply wet (robust) and the free surface",
            "/ follows the bed slope. Used by the vertical convergence study.",
            "INITIAL CONDITIONS : 'CONSTANT DEPTH'",
            f"INITIAL DEPTH : {vd.depth:.3f}",
            f"/ TELEMAC v9+ true hotstart instead: FILE FOR 2D CONTINUATION : {cfg.results_slf}",
        ]
    else:  # "continuation" - TELEMAC v9+ true 2D -> 3D hotstart
        hotstart_lines = [
            "/ HOTSTART from the 2D steady run (TELEMAC v9+): FILE FOR 2D CONTINUATION",
            "/ lifts the 3D free surface + horizontal velocities from the final 2D state",
            "/ (vertical velocity 0; horizontal velocity copied up each column). In v9",
            "/ this keyword alone triggers the continuation (no old '2D CONTINUATION').",
            f"FILE FOR 2D CONTINUATION : {cfg.results_slf}",
            "FILE FOR 2D CONTINUATION FORMAT : 'SERAFIN'",
            "INITIAL TIME SET TO ZERO : YES",
            "/ FALLBACK if telemac3d aborts with 'DEBIMP_3D: PROBLEM ON BOUNDARY' (the",
            "/ continuation inflow depth is too thin/supercritical for the prescribed Q):",
            "/ comment the three lines above and uncomment this uniform cold-start seed:",
            "/ INITIAL CONDITIONS : 'CONSTANT DEPTH'",
            f"/ INITIAL DEPTH : {vd.depth:.3f}",
        ]

    pressure = "non-hydrostatic" if non_hydrostatic else "hydrostatic"
    lines: list[str] = [
        "/" + "-" * 68,
        f"/ TELEMAC3D steering generated by axqua for case '{cfg.name}'",
        f"/ {pressure}; hotstarted from the 2D steady result; sigma layers",
        "/" + "-" * 68,
        f"TITLE : '{cfg.name} 3D {pressure}'",
        "/",
        "/ INPUT / OUTPUT FILES",
        f"GEOMETRY FILE : {cfg.geometry_slf}",
        f"BOUNDARY CONDITIONS FILE : {cfg.boundary_cli}",
        # user-fortran overrides (compiled + linked in) when a `user_fortran/`
        # directory is present in the model dir - e.g. the KB15 Spalart-Allmaras
        # source-term fix (sousa.f wall distance + dry-cell source clip) so 3D
        # turbulence produces physical eddy viscosity. See user_fortran/README.
        *([f"FORTRAN FILE : {(model / 'user_fortran').name}"]
          if (model / "user_fortran").is_dir() else []),
        # hydrograph forcing (Q(t)/SL(t)) for the unsteady run
        *([f"LIQUID BOUNDARIES FILE : {liquid_boundaries_file}"]
          if (unsteady and liquid_boundaries_file) else []),
        f"3D RESULT FILE : {results3d_name or cfg.results3d_slf}",
        f"2D RESULT FILE : {results2d_name or cfg.results2d_from_3d_slf}",
        "MASS-BALANCE : YES",
        # per-boundary flux printouts in the listing - what the boundary-flux
        # convergence analysis (axqua.flux_convergence) reads
        "PRINTING CUMULATED FLOWRATES : YES",
        "/",
        *hotstart_lines,
        "VARIABLES FOR 2D GRAPHIC PRINTOUTS : 'U,V,H,S,B,F'",
        "VARIABLES FOR 3D GRAPHIC PRINTOUTS : 'Z,U,V,W,NUX,NUZ'",
        "/",
        "/ VERTICAL MESH (sigma planes - TELEMAC has no constant-dz z-layers; the",
        f"/ level count targets dz ~ dx/{TARGET_ASPECT:g} with dx/dz <= {MAX_ASPECT:g})",
        f"/ {vd.reason}",
        f"NUMBER OF HORIZONTAL LEVELS : {vd.n_levels}",
        "MESH TRANSFORMATION : 1",
        "/",
        "/ TIME (3D has no DESIRED COURANT NUMBER; the fixed step targets Courant "
        f"{TARGET_COURANT:g})",
        f"/ dt = {TARGET_COURANT:g} * min(dx={vd.dx:.2f}, dz={vd.dz:.2f}) / "
        f"u~{u_char:.2f} m/s",
        f"TIME STEP : {dt}",
        f"NUMBER OF TIME STEPS : {n_steps}",
        f"GRAPHIC PRINTOUT PERIOD : {graphic}",
        f"LISTING PRINTOUT PERIOD : {listing}",
        # NB: TELEMAC-3D has NO steady-state auto-stop (the 2D 'STOP IF A STEADY STATE
        # IS REACHED' / 'STOP CRITERIA' keywords do not exist here), so the run always
        # marches the full NUMBER OF TIME STEPS above - size it for the spin-up.
        "/",
        f"/ PRESSURE ({pressure})",
        f"NON-HYDROSTATIC VERSION : {'YES' if non_hydrostatic else 'NO'}",
        # the pressure-Poisson keywords only apply to the non-hydrostatic solver;
        # 1e-4 is the documented v8.1+ default and is plenty for a spin-up/hotstart
        # (tighter 1e-6/1e-8 tolerances just waste iterations).
        *(["SOLVER FOR PPE : 7", "ACCURACY FOR PPE : 1.E-4"]
          if non_hydrostatic else []),
        # use the previous water depth as the initial guess to cut solver work
        "INITIAL GUESS FOR DEPTH : 1",
        # segment-wise (edge-based) matrix storage - the recommended efficient option
        "MATRIX STORAGE : 3",
        "/",
        "/ NUMERICS (finite elements - TELEMAC-3D has no finite-volume kernel)",
        # MURD PSI (5) with the locally-implicit option (4) is the consistent choice
        # for wetting/drying (TIDAL FLATS: YES): robust on tidal flats without the
        # sub-iteration blow-up the default MURD option hits on coarse/parallel meshes.
        "SCHEME FOR ADVECTION OF VELOCITIES : 5",
        "SCHEME OPTION FOR ADVECTION OF VELOCITIES : 4",
        "SCHEME FOR ADVECTION OF K-EPSILON : 5",
        "SCHEME OPTION FOR ADVECTION OF K-EPSILON : 4",
        "SOLVER FOR PROPAGATION : 2",
        # relax the propagation solver tolerance from the 1e-8 default so the CG
        # converges within its iteration budget on these meshes.
        "ACCURACY FOR PROPAGATION : 1.E-6",
        "MAXIMUM NUMBER OF ITERATIONS FOR PROPAGATION : 200",
        f"FREE SURFACE GRADIENT COMPATIBILITY : {cfg.hydrodynamics.free_surface_gradient_compat}",
        "IMPLICITATION FOR DEPTH : 0.55",
        "IMPLICITATION FOR VELOCITIES : 0.55",
        "TIDAL FLATS : YES",
        # mass-lumping + flux-controlled negative depths is the robust wetting/drying
        # combination (treatment 2 requires mass-lumping on depth).
        "MASS-LUMPING FOR DEPTH : 1.",
        "MASS-LUMPING FOR VELOCITIES : 1.",
        "TREATMENT OF NEGATIVE DEPTHS : 2",
        # clip the depth on dry nodes: the 2D hotstart carries zero-depth (dry)
        # floodplain columns, and the sigma transform divides by the column depth -
        # without a positive floor that is a division by zero (SIGFPE) at init.
        f"MINIMAL VALUE FOR DEPTH : {depth_floor}",
        "/",
        f"/ TURBULENCE ({turb_reason})",
        f"HORIZONTAL TURBULENCE MODEL : {h_turb}",
        f"VERTICAL TURBULENCE MODEL : {v_turb}",
        *(["MIXING LENGTH MODEL : 3"] if v_turb == 2 else []),
        "/",
        "/ FRICTION (single representative Nikuradse law; TELEMAC-3D has no zonal",
        "/ FRICTION DATA FILE, so the 2D per-zone roughness is collapsed to ONE ks -",
        "/ NOT spatially equivalent to the 2D setup; refine here if zonation matters)",
        f"LAW OF BOTTOM FRICTION : {law}",
        f"FRICTION COEFFICIENT FOR THE BOTTOM : {coef}",
    ]

    if flow or elev:
        lines += ["/", "/ BOUNDARY CONDITIONS (reused from the 2D steering for the",
                  "/ same horizontal inflow/outflow behaviour)"]
        if flow:
            lines.append(f"PRESCRIBED FLOWRATES : {flow}")
        if elev:
            lines.append(f"PRESCRIBED ELEVATIONS : {elev}")
        # continuation reuses the 2D horizontal velocity profiles; the cold-start
        # mode leaves them at the constant-normal default (robust for the deep seed)
        if hotstart == "continuation" and prof:
            lines.append(f"VELOCITY PROFILES : {prof}")
        if n_liquid:
            # logarithmic vertical velocity profile on every liquid boundary
            lines.append("VELOCITY VERTICAL PROFILES : " + ";".join(["2"] * n_liquid))

    if cfg.morphodynamics.enabled and gaia_cas:
        # GAIA couples to telemac3d the same way as 2D (bedload / suspended load);
        # suspended sediment is carried as tracers declared through the coupling.
        lines += ["/", "/ MORPHODYNAMICS (GAIA - bedload / suspended load)",
                  "COUPLING WITH : 'GAIA'",
                  f"GAIA STEERING FILE : {gaia_cas}",
                  f"COUPLING PERIOD FOR GAIA : {cfg.morphodynamics.coupling_period}"]

    lines.append("&ETA")
    path = model / (out_name or cas3d_name(cfg))
    path.write_text("\n".join(lines) + "\n")
    return ThreeDSetup(
        cas=path, n_levels=vd.n_levels, dz=vd.dz, dx=vd.dx, depth=vd.depth,
        time_step=dt, n_time_steps=n_steps, h_turbulence=h_turb,
        v_turbulence=v_turb, courant=TARGET_COURANT,
        turbulence_reason=turb_reason, layers_reason=vd.reason,
        non_hydrostatic=non_hydrostatic,
    )


def build_3d_cases(cfg: Config,
                   hydrostatic_steps: int | None = None,
                   hydrostatic_listing: int | None = None) -> dict:
    """Write the three 3D steering files ``add3d.py`` provides and return their
    setups as ``{"hydrostatic": ThreeDSetup, "hydrodyn": ThreeDSetup,
    "unsteady": Unsteady3DSetup | None}``.

    1. ``hotstart3d_hydrostatic.cas`` - ``NON-HYDROSTATIC VERSION : NO`` (the cheap
       hydrostatic solver), constant in-file Q/H, marching *hydrostatic_steps*
       (default :data:`HYDROSTATIC_N_STEPS`) fixed steps with a short listing period
       (*hydrostatic_listing*, default :data:`HYDROSTATIC_LISTING_PERIOD`) so the
       sortie carries enough ``FLUX BOUNDARY`` printouts to check **when the boundary
       fluxes converge** in a steady telemac3d run.
    2. ``hotstart3d_hydrodyn.cas`` - the steady **non-hydrostatic** run with the same
       in-file prescribed Q (inflow) and H (outflow).
    3. ``unsteady3d.cas`` - non-hydrostatic, hydrograph-driven: Q(t) inflow and the
       stage-discharge outflow SL(t) come from the same ``LIQUID BOUNDARIES FILE``
       as ``unsteady2d.cas``. Requires a *varying* ``boundaries.inflow`` series;
       ``None`` (with a log notice) otherwise.

    All three hotstart from the 2D steady result (v9 2D->3D continuation) and share
    the sigma-level count, turbulence closure and Courant-sized time step inferred
    from **one** read of ``r2d.slf``.
    """
    from axqua.solvers.telemac.unsteady import build_unsteady_3d_case

    data = selafin.read_slf(Path(cfg.model_dir) / cfg.results_slf)
    setups: dict = {
        "hydrostatic": build_3d_cas(
            cfg, data=data, non_hydrostatic=False,
            n_time_steps=hydrostatic_steps or HYDROSTATIC_N_STEPS,
            listing_period=hydrostatic_listing or HYDROSTATIC_LISTING_PERIOD,
            out_name=HYDROSTATIC_CAS,
            results3d_name=HYDROSTATIC_RESULT_3D,
            results2d_name=HYDROSTATIC_RESULT_2D),
        "hydrodyn": build_3d_cas(cfg, data=data, out_name=HYDRODYN_CAS),
    }
    try:
        setups["unsteady"] = build_unsteady_3d_case(cfg, out_name=UNSTEADY3D_CAS,
                                                    data=data)
    except Exception as exc:  # noqa: BLE001 - a missing hydrograph is a valid state
        setups["unsteady"] = None
        log.info("%s skipped: %s", UNSTEADY3D_CAS, exc)
    return setups
