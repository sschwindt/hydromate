"""``constant/`` and ``system/`` dictionaries for the interFoam case.

This module is where the air phase is dealt with. ``interFoam`` in OpenFOAM 9 is
already a PIMPLE solver, so the question is never "which solver" but "which
settings", and the settings that matter are these four:

1. **``MULESCorr yes`` (semi-implicit MULES).** With the default explicit MULES the
   phase-fraction equation carries its own Courant limit and the tutorials run at
   ``maxAlphaCo 0.2``. The semi-implicit limiter removes that constraint, so the
   time step is set by the flow rather than by the interface and ``maxCo`` /
   ``maxAlphaCo`` can go to ~0.9 - a three- to four-fold larger step for the same
   physics.
2. **``limitVelocity`` in ``system/fvConstraints``.** Air is a thousand times
   lighter than water, so a small pressure error accelerates it enormously; the
   resulting air jet then sets the Courant number and the time step collapses on a
   phase nobody is interested in. Capping ``|U|`` at a few times the expected water
   velocity stops that dead. Water never reaches the cap, so the water solution is
   untouched.
3. **``momentumPredictor no``.** Standard for gravity-driven free-surface flow: the
   momentum predictor buys nothing when buoyancy dominates and costs an extra solve.
4. **``nNonOrthogonalCorrectors 2``.** A terrain-following mesh *is* non-orthogonal
   wherever the bed slopes; without the correctors the pressure solution leaks that
   as spurious velocity near the bed.

The lid clamping that removes most air cells altogether is in
:mod:`hydromate.solvers.openfoam.mesh`, and matters more than all four.

Staging
-------
Two dict sets are written, under ``system/stage1-spinup/`` and
``system/stage2-run/``; :mod:`hydromate.solvers.openfoam.runtime` copies one into
``system/`` to select it. Stage 1 settles the interface from the 2D hotstart at a
tight Courant number with diffusive schemes; stage 2 restarts from it and runs the
production settings. The split exists because the first seconds after a hotstart are
the least like the converged flow - the 2D state is depth-averaged, so there is no
vertical structure at all at t = 0 - and paying for robustness there is far cheaper
than paying for it over the whole run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hydromate.solvers.openfoam.polymesh import foam_footer, foam_header

log = logging.getLogger("hydromate")

STAGE_DIRS = {"spinup": "stage1-spinup", "run": "stage2-run"}
# system/ files that differ between the two stages and are therefore staged
STAGED_FILES = ("controlDict", "fvSchemes", "fvSolution")


@dataclass
class Stage:
    """One run stage's numerics."""

    name: str
    start_from: str          # startTime | latestTime
    end_time: float
    max_courant: float
    alpha_scheme: str        # the div(phi,alpha) scheme
    velocity_scheme: str     # the div(rhoPhi,U) scheme
    n_outer_correctors: int
    purpose: str


def stages(cfg) -> list[Stage]:
    of = cfg.openfoam
    return [
        Stage(name="spinup", start_from="startTime", end_time=of.spinup_time,
              max_courant=of.spinup_courant,
              # HALF the interface compression of the production stage. In OpenFOAM 9
              # cAlpha lives in the scheme, not in fvSolution, so this is where it is
              # set. Compressing hard while the hotstart still has no vertical
              # velocity structure sharpens the interface into an instability;
              # dropping compression altogether instead smears it over 30 s of
              # spin-up, which is just as unhelpful.
              alpha_scheme="Gauss interfaceCompression vanLeer 0.5",
              velocity_scheme="Gauss upwind",
              n_outer_correctors=3,
              purpose="settle the interface and the vertical profile from the "
                      "depth-averaged 2D hotstart"),
        Stage(name="run", start_from="latestTime", end_time=of.end_time,
              max_courant=of.max_courant,
              alpha_scheme="Gauss interfaceCompression vanLeer 1",
              velocity_scheme="Gauss limitedLinearV 1",
              n_outer_correctors=2,
              purpose="production run at the full Courant number"),
    ]


def _dict_file(obj: str, body: str, *, location: str, cls: str = "dictionary") -> str:
    return foam_header(cls, obj, location=location) + body + foam_footer()


# --------------------------------------------------------------------------- #
# constant/
# --------------------------------------------------------------------------- #


def transport_properties(cfg) -> str:
    of = cfg.openfoam
    body = f"""phases (water air);

water
{{
    transportModel  Newtonian;
    nu              {of.water_viscosity:g};
    rho             {of.water_density:g};
}}

air
{{
    transportModel  Newtonian;
    nu              {of.air_viscosity:g};
    rho             {of.air_density:g};
}}

sigma           {of.surface_tension:g};
"""
    return _dict_file("transportProperties", body, location="constant")


def momentum_transport(cfg) -> str:
    of = cfg.openfoam
    if of.turbulence == "laminar":
        body = "simulationType  laminar;\n"
    else:
        body = f"""simulationType  RAS;

RAS
{{
    model           {of.turbulence};

    turbulence      on;

    printCoeffs     on;
}}
"""
    return _dict_file("momentumTransport", body, location="constant")


def gravity() -> str:
    """``constant/g``. z is up: the mesh is in a projected CRS with real elevations."""
    body = "dimensions      [0 1 -2 0 0 0 0];\nvalue           (0 0 -9.81);\n"
    return _dict_file("g", body, location="constant",
                      cls="uniformDimensionedVectorField")


# --------------------------------------------------------------------------- #
# system/
# --------------------------------------------------------------------------- #


def control_dict(cfg, stage: Stage, *, patches: list[str]) -> str:
    of = cfg.openfoam
    body = f"""application     {of.solver};

startFrom       {stage.start_from};

startTime       0;

stopAt          endTime;

endTime         {stage.end_time:g};

deltaT          {of.initial_time_step:g};

writeControl    adjustableRunTime;

writeInterval   {of.write_interval:g};

purgeWrite      0;

// binary keeps a multi-million-cell case's output and the decomposed meshes
// compact; only the hand-written polyMesh is ASCII
writeFormat     binary;

writePrecision  8;

writeCompression off;

timeFormat      general;

timePrecision   6;

runTimeModifiable yes;

adjustTimeStep  yes;

// with MULESCorr the alpha equation is no longer explicitly Courant-limited, so
// maxAlphaCo can match maxCo instead of the tutorials' 0.2
maxCo           {stage.max_courant:g};
maxAlphaCo      {stage.max_courant:g};

maxDeltaT       {of.max_time_step:g};

functions
{{
{_flux_functions(patches, monitor_interval(of))}
}}
"""
    return _dict_file("controlDict", body, location="system")


def monitor_interval(of) -> float:
    """Simulated-time spacing of the discharge monitors [s].

    A tenth of the field write interval, so a run writing ten result frames leaves a
    hundred discharge samples - enough for the ten-sample steady window
    :mod:`hydromate.solvers.openfoam.report` looks for, without a line per time step.
    """
    return max(of.write_interval / 10.0, 1e-6)


def _flux_functions(patches: list[str], interval: float) -> str:
    """``surfaceFieldValue`` monitors giving the WATER discharge through each patch.

    ``phi`` is the total volumetric flux, water and air together; weighting it by
    ``alpha.water`` (a documented ``surfaceFieldValue`` option in OpenFOAM 9) gives
    the water discharge alone. That is the quantity the run has to converge on, and
    the one :mod:`hydromate.solvers.openfoam.report` reads back - the direct analogue of the
    boundary-flux balance :mod:`hydromate.flux_convergence` reads out of a TELEMAC
    listing.
    """
    blocks = []
    for patch in patches:
        blocks.append(f"""    Q_{patch.replace('-', '_')}
    {{
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        // runTime, NOT timeStep: with an adaptive dt a per-step interval samples
        // the history unevenly in time - dense during the stiff opening, sparse
        // once the run speeds up - which is the opposite of what a convergence
        // history needs. 'runTime' writes at the first step past each interval and,
        // unlike 'adjustableRunTime', does not constrain dt to land on it.
        writeControl    runTime;
        writeInterval   {interval:g};
        log             no;
        writeFields     no;
        regionType      patch;
        name            {patch};
        operation       sum;
        weightField     alpha.water;
        fields          (phi);
    }}""")
    return "\n\n".join(blocks)


def fv_schemes(cfg, stage: Stage) -> str:
    body = f"""ddtSchemes
{{
    default         Euler;
}}

gradSchemes
{{
    default         Gauss linear;
}}

divSchemes
{{
    div(rhoPhi,U)   {stage.velocity_scheme};
    div(phi,alpha)  {stage.alpha_scheme};
    div(phi,k)      Gauss upwind;
    div(phi,omega)  Gauss upwind;
    div(phi,epsilon) Gauss upwind;
    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;
}}

laplacianSchemes
{{
    // 'limited corrected 0.33' rather than plain 'corrected': the terrain-following
    // mesh is non-orthogonal wherever the bed slopes, and the limiter keeps the
    // explicit correction from over-shooting there
    default         Gauss linear limited corrected 0.33;
}}

interpolationSchemes
{{
    default         linear;
}}

snGradSchemes
{{
    default         limited corrected 0.33;
}}

// kOmegaSST needs the distance to the nearest wall. 'meshWave' is the exact
// (front-marching) method; on a bed-following mesh the cheap 'Poisson'
// approximation is noticeably wrong in the thin near-bed layers, which is exactly
// where the blending function it feeds decides between k-omega and k-epsilon.
wallDist
{{
    method          meshWave;
}}
"""
    return _dict_file("fvSchemes", body, location="system")


def fv_solution(cfg, stage: Stage) -> str:
    body = f"""solvers
{{
    "alpha.water.*"
    {{
        nAlphaCorr      2;
        nAlphaSubCycles 1;

        // semi-implicit MULES: this is what decouples the time step from the
        // interface Courant number and lets maxAlphaCo run at {stage.max_courant:g}
        MULESCorr       yes;
        nLimiterIter    5;
        alphaApplyPrevCorr yes;

        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-9;
        relTol          0;
    }}

    "pcorr.*"
    {{
        solver          PCG;
        preconditioner  DIC;
        tolerance       1e-5;
        relTol          0;
    }}

    p_rgh
    {{
        // GAMG, not PCG: on a multi-million-cell mesh the pressure solve dominates
        // and a geometric-algebraic multigrid is several times faster
        solver          GAMG;
        tolerance       1e-7;
        relTol          0.01;
        smoother        DIC;
    }}

    p_rghFinal
    {{
        $p_rgh;
        tolerance       1e-8;
        relTol          0;
    }}

    "(U|k|omega|epsilon).*"
    {{
        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-7;
        relTol          0.1;
    }}

    "(U|k|omega|epsilon)Final"
    {{
        $U;
        relTol          0;
    }}
}}

PIMPLE
{{
    // buoyancy dominates in a free-surface flow: the momentum predictor costs an
    // extra solve and buys nothing
    momentumPredictor no;

    nOuterCorrectors {stage.n_outer_correctors};
    nCorrectors     3;

    // the bed-following mesh is non-orthogonal where the bed is steep; without
    // these the pressure solution leaks that as spurious near-bed velocity
    nNonOrthogonalCorrectors 2;
}}

relaxationFactors
{{
    equations
    {{
        ".*"            1;
    }}
}}
"""
    return _dict_file("fvSolution", body, location="system")


def fv_constraints(cfg, velocity_cap: float) -> str:
    body = f"""// Cap on the velocity magnitude, the single most effective stop on the AIR phase
// destroying the time step. Air is ~1000x lighter than water, so a small pressure
// error accelerates it enormously; the resulting jet then sets the Courant number
// and the step collapses on a phase this model does not care about.
//
// {velocity_cap:.2f} m/s is several times the water velocity this reach carries, so
// the water solution never reaches the cap and is untouched by it. Raise it if a
// legitimate jet (a steep chute, a weir nappe) is being clipped - the run log
// reports how often the constraint bites.
limitU
{{
    type            limitVelocity;

    selectionMode   all;

    max             {velocity_cap:.4g};
}}
"""
    return _dict_file("fvConstraints", body, location="system")


def decompose_par_dict(cfg) -> str:
    n = max(int(cfg.openfoam.n_processors), 1)
    body = f"""numberOfSubdomains {n};

// 'scotch' rather than 'simple'/'hierarchical': the wetted corridor is a sinuous,
// non-convex block, so a geometric split would hand some processors mostly empty
// space. Scotch balances by cell count and minimises the interface.
method          scotch;
"""
    return _dict_file("decomposeParDict", body, location="system")


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #


def write_dicts(of_mesh, cfg, case_dir: str | Path, *,
                velocity_cap: float) -> list[Path]:
    """Write ``constant/`` and ``system/`` (both staged dict sets)."""
    case_dir = Path(case_dir)
    constant = case_dir / "constant"
    system = case_dir / "system"
    constant.mkdir(parents=True, exist_ok=True)
    system.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for name, text in (("transportProperties", transport_properties(cfg)),
                       ("momentumTransport", momentum_transport(cfg)),
                       ("g", gravity())):
        written.append(_write(constant / name, text))

    written.append(_write(system / "decomposeParDict", decompose_par_dict(cfg)))
    written.append(_write(system / "fvConstraints", fv_constraints(cfg, velocity_cap)))

    patches = of_mesh.inlet_patches + of_mesh.outlet_patches
    for stage in stages(cfg):
        stage_dir = system / STAGE_DIRS[stage.name]
        stage_dir.mkdir(parents=True, exist_ok=True)
        written.append(_write(stage_dir / "controlDict",
                              control_dict(cfg, stage, patches=patches)))
        written.append(_write(stage_dir / "fvSchemes", fv_schemes(cfg, stage)))
        written.append(_write(stage_dir / "fvSolution", fv_solution(cfg, stage)))

    # start on stage 1 so the case is runnable straight out of the build
    activate(case_dir, "spinup")
    return written


def activate(case_dir: str | Path, stage: str, cfg=None) -> None:
    """Make one stage's dictionaries the active ``system/`` set.

    With *cfg* the three staged files are **regenerated from that config** before
    being activated, so a run always honours the configuration it was launched with.
    Without it they would come from the build, and changing ``end_time`` or
    ``max_courant`` and re-running ``openfoam_run.py`` would silently run the old
    values while every message reported the new ones - a trap precisely because
    nothing about it looks wrong.

    The patch names are read back from the written mesh
    (:func:`hydromate.solvers.openfoam.polymesh.read_patch_names`), so regenerating needs no
    remeshing and no state carried over from the build.
    """
    import shutil

    from hydromate.solvers.openfoam.polymesh import read_patch_names

    case_dir = Path(case_dir)
    system = case_dir / "system"
    src = system / STAGE_DIRS[stage]

    if cfg is not None:
        target = next((s for s in stages(cfg) if s.name == stage), None)
        if target is None:
            raise ValueError(f"unknown stage {stage!r}; expected one of "
                             f"{sorted(STAGE_DIRS)}")
        patches = [p for p in read_patch_names(case_dir)
                   if p.startswith(("inlet", "outlet"))]
        src.mkdir(parents=True, exist_ok=True)
        (src / "controlDict").write_text(control_dict(cfg, target, patches=patches))
        (src / "fvSchemes").write_text(fv_schemes(cfg, target))
        (src / "fvSolution").write_text(fv_solution(cfg, target))

    if not src.is_dir():
        raise FileNotFoundError(
            f"no {stage!r} stage in {src}; rebuild the case with "
            "openfoam_preprocessing.py")
    for name in STAGED_FILES:
        shutil.copyfile(src / name, system / name)
    log.info("activated the %s stage (%s)", stage, STAGE_DIRS[stage])


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path
