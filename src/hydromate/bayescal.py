"""HydroBayesCal integration: build inputs, emit config, launch calibration.

Keeps the per-case ``run_Bayes_cal.py`` / ``run_Bayes_cal_multiflow.py`` scripts
thin - they only declare case-specific choices (calibration target, and for the
multi-flow case the list of :class:`FlowSpec`) and call
:func:`run_single_flow_calibration` / :func:`run_multiflow_calibration`.

Two calibration modes:

* **single-flow** - one steady discharge, ground truth compiled from the
  configured ``ground_truth.sources`` (see :mod:`hydromate.ground_truth`); the
  case's built ``.cas`` is calibrated directly with HydroBayesCal's stock
  ``bal_telemac.py``.
* **multi-flow** - several steady discharges, each with its own field campaign
  (:mod:`hydromate.campaigns`) and its own steering file derived from the built
  base case; run with the additive ``bal_telemac_multiflow.py`` +
  ``MultiflowTelemacModel``. One shared roughness is calibrated against all
  campaigns jointly.

**HydroBayesCal is a pip dependency** (``pip install 'hydromate[calibration]'``),
not an external checkout: the drivers ship with the package from 1.4.2 on
(``hydroBayesCal.copy_driver``), so there is no directory to point at and no
second conda environment to name. It is an *optional* extra because its surrogate
stack - bayesvalidrox, gpytorch, scikit-learn - is heavy and irrelevant to
building or running a TELEMAC case, so :func:`require_hbc` imports it lazily and
explains the install if it is missing.

A staged driver is still run as a **subprocess**, but in this interpreter's own
environment: the driver is a script with an ``argparse`` ``main()``, and it must
run with TELEMAC sourced (every surrogate iteration launches the solver), which
mutates the environment in ways not to inflict on the caller's process. Pass
``checkout=`` to develop against an unreleased driver from a source tree.

Lessons baked in (see the module functions):

* the built ``.cas`` must output ``SCALAR VELOCITY`` (graphic printout ``M``);
* the staged driver extracts the **last** (converged) SELAFIN frame, not the
  ``mean_last`` window that would average a dry-start transient;
* HydroBayesCal's prior-sample count drives a per-location dense covariance in
  gpytorch, so it is kept modest (``calibration.prior_samples``, default 5000) -
  ample for a low-dimensional roughness posterior and safe on a 16 GB box;
* per-flow steering files differ only in Q, the outflow stage (from the
  synthesised rating at that Q), ``DURATION`` and the results name.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from hydromate import campaigns
from hydromate.config import Config
from hydromate.ground_truth import compile_ground_truth, read_tidy
from hydromate.rating import synthesize_outflow_rating


# --------------------------------------------------------------------------- #
# install location
# --------------------------------------------------------------------------- #
def require_hbc():
    """Import HydroBayesCal, or explain how to install it.

    It is an **optional dependency** (``pip install hydromate[calibration]``): the
    surrogate stack it pulls in - bayesvalidrox, gpytorch, scikit-learn - is heavy and
    irrelevant to building or running a TELEMAC case, which is what most of hydromate
    does. So it is imported where it is used rather than at module import.
    """
    try:
        import hydroBayesCal
    except ImportError as exc:                       # pragma: no cover - install aid
        raise SystemExit(
            "HydroBayesCal is not installed. It is an optional dependency:\n"
            "    pip install 'hydromate[calibration]'\n"
            "or, for a local checkout you are also developing:\n"
            "    pip install -e /path/to/hydrobayescal\n"
            f"(import failed: {exc})"
        ) from exc
    if not hasattr(hydroBayesCal, "copy_driver"):
        raise SystemExit(
            "The installed HydroBayesCal is too old: it does not ship the calibration "
            "drivers with the package (hydroBayesCal.copy_driver is missing). "
            "Upgrade with:  pip install -U 'hydrobayescal>=1.4.4'"
        )
    return hydroBayesCal


def _hbc_version() -> str:
    """Installed HydroBayesCal version, for the staging log."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("hydrobayescal")
    except PackageNotFoundError:                     # pragma: no cover
        return "unknown"


# --------------------------------------------------------------------------- #
# shared HydroBayesCal plumbing
# --------------------------------------------------------------------------- #
def ensure_graphic_output(cas_path: Path, code: str = "M") -> bool:
    """Ensure the ``.cas`` writes a graphic-printout variable (default ``M`` =
    SCALAR VELOCITY). Returns True if the file was patched. HydroBayesCal reads
    the quantity straight from the results SELAFIN, so it must be printed.
    """
    cas_path = Path(cas_path)
    if not cas_path.exists():
        raise SystemExit(f"no built case at {cas_path}; run preprocessing.py first")
    import re
    text = cas_path.read_text()
    m = re.search(r"(VARIABLES FOR GRAPHIC PRINTOUTS\s*:\s*')([^']*)(')", text)
    if not m:
        print(f"  warning: no VARIABLES FOR GRAPHIC PRINTOUTS in {cas_path.name}")
        return False
    codes = [c.strip() for c in m.group(2).split(",") if c.strip()]
    if code in codes:
        return False
    codes.append(code)
    cas_path.write_text(text[:m.start()] + m.group(1) + ",".join(codes)
                        + m.group(3) + text[m.end():])
    print(f"  added graphic printout {code!r} to {cas_path.name}")
    return True


def stage_driver(out_dir: Path, template_name: str, *, checkout: Path | None = None,
                 also: tuple[str, ...] = ()) -> Path:
    """Copy a HydroBayesCal driver into ``out_dir`` with the extraction window
    patched ``mean_last`` -> ``last``.

    The driver comes from the **installed** HydroBayesCal package
    (``hydroBayesCal.copy_driver``, which also brings any sibling a driver imports),
    so there is no checkout path to configure. *checkout* overrides that with a
    source tree, for developing against an unreleased driver.

    The extraction window is patched because the stock drivers average the last
    *window* of SELAFIN frames (``mean_last``); on a run that marches to steady state
    from a dry or pre-wetted start, that averages the residual transient into the
    calibration values. ``last`` takes the converged frame. *also* names further
    drivers to place beside it (kept for callers that name the companion explicitly;
    ``copy_driver`` already brings known siblings).

    **Every** staged file is patched, not just the primary one. That is not caution:
    ``bal_telemac_multiflow.py`` imports ``run_complex_model`` / ``run_bal_model``
    from its ``bal_telemac.py`` sibling and calls them *without* passing
    ``output_extraction_time``, so a multi-flow run takes the sibling's default. With
    only the primary patched, multi-flow calibrations silently averaged the transient
    - the exact failure this patch exists to prevent - while single-flow ones did not.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if checkout is not None:
        checkout = Path(checkout)

        def _find(name):
            candidates = (checkout / name,
                          checkout / "templates" / name,
                          checkout / "src" / "hydroBayesCal" / "drivers" / name)
            return next((p for p in candidates if p.is_file()), None)

        primary = _find(template_name)
        if primary is None:
            raise SystemExit(
                f"HydroBayesCal driver {template_name!r} not found in {checkout}")
        for name in also:
            src = _find(name)
            if src is None:
                raise SystemExit(f"companion driver {name!r} not found in {checkout}")
            shutil.copy2(src, out_dir / name)
        source = "checkout"
    else:
        hbc = require_hbc()
        hbc.copy_driver(template_name, out_dir)
        for name in also:
            hbc.copy_driver(name, out_dir)
        primary = out_dir / template_name
        source = f"hydroBayesCal {_hbc_version()}"

    if checkout is not None:
        # the checkout branch has not copied the primary yet
        shutil.copy2(primary, out_dir / template_name)

    patched = 0
    for staged in sorted(out_dir.glob("*.py")):
        text = staged.read_text()
        n = text.count('output_extraction_time="mean_last"')
        if n:
            staged.write_text(text.replace('output_extraction_time="mean_last"',
                                           'output_extraction_time="last"'))
            patched += n
    print(f"staged {template_name} from {source}"
          + (f" (+{', '.join(also)})" if also else "")
          + f" -> {out_dir}"
          + (f" (extraction mean_last -> last, {patched} sites)" if patched else ""))
    return out_dir / template_name


def other_calibration_running(model_dir: Path) -> bool:
    """A TELEMAC run from this model dir already active (would fight over the
    shared friction.tbl)."""
    probe = subprocess.run(["pgrep", "-fa", "telemac2d"], capture_output=True, text=True)
    return str(model_dir) in probe.stdout or "steady2d" in probe.stdout


def launch(cfg: Config, driver: Path, config_path: Path, *, env: str | None = None,
           note: str = "HydroBayesCal calibration") -> int:
    """Run a staged driver, in **this** interpreter's environment.

    HydroBayesCal is a dependency of hydromate, so it is importable here - there is
    no second conda environment to name and no interpreter to guess. The driver is
    still run as a subprocess rather than imported, for two reasons that matter over
    a calibration lasting hours: it is a script with module-level state and an
    ``argparse`` ``main()``, and it has to run **with TELEMAC sourced**, since every
    surrogate iteration launches the solver. Sourcing mutates the environment, which
    is exactly the sort of thing not to do to the caller's own process.

    ``sys.executable`` is used explicitly so the subshell cannot pick up a different
    ``python`` from the sourced TELEMAC environment.

    *env* is accepted and ignored; it named the separate conda environment this used
    to need. Returns the driver's exit code.
    """
    if env:
        print(f"note: env={env!r} ignored - HydroBayesCal now runs in hydromate's own "
              "environment (it is a dependency), so no separate env is needed")
    inner = (f"source {shlex.quote(str(cfg.telemac.pysource))}; "
             f"cd {shlex.quote(str(driver.parent))}; "
             f"{shlex.quote(sys.executable)} -u {shlex.quote(driver.name)} "
             f"--config {shlex.quote(str(config_path))}")
    print(f"\nlaunching {note}:\n  {inner}\n")
    return subprocess.run(["bash", "-lc", "set -e; " + inner]).returncode


# --------------------------------------------------------------------------- #
# single-flow calibration
# --------------------------------------------------------------------------- #
def build_velocity_csv(cfg: Config, *, vel_err_floor: float = campaigns.VELOCITY_ERROR_FLOOR,
                       depth_err_floor: float = campaigns.DEPTH_ERROR_FLOOR
                       ) -> tuple[Path, pd.DataFrame]:
    """Compile the configured ground truth and write the calibration-points CSV.

    The velocity target is the horizontal FlowTracker speed (0.6-depth proxy for
    the depth-averaged velocity) with propagated, floored error; water depth is
    carried alongside. Reads ``ground_truth.sources`` via
    :func:`hydromate.ground_truth.compile_ground_truth`.
    """
    compile_ground_truth(cfg)
    tables = read_tidy(cfg.ground_truth_path)
    if "hydraulics" not in tables:
        raise SystemExit(f"no 'hydraulics' tab in {cfg.ground_truth_path}; check "
                         "ground_truth.sources in case-config.yml")
    df = tables["hydraulics"].reset_index(drop=True)
    for col in ("x", "y", "u", "v", "h"):
        if col not in df.columns:
            raise SystemExit(f"hydraulics ground truth missing '{col}' (has {list(df.columns)})")
    out = campaigns.assemble_velocity_csv(
        df["x"], df["y"], df["z"] if "z" in df.columns else 0.0,
        df["u"], df["v"],
        df["u_err"] if "u_err" in df.columns else 0.0,
        df["v_err"] if "v_err" in df.columns else 0.0,
        df["h"], [f"pt-{i + 1}" for i in range(len(df))],
        vel_err_floor, depth_err_floor).drop(columns="label")
    path = cfg.calibration_path(cfg.calibration_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return path, out


def run_single_flow_calibration(
        cfg: Config, *, calibration_quantities=("SCALAR VELOCITY",),
        extraction_quantities=("WATER DEPTH", "SCALAR VELOCITY"),
        vel_err_floor: float = campaigns.VELOCITY_ERROR_FLOOR,
        depth_err_floor: float = campaigns.DEPTH_ERROR_FLOOR,
        prepare_only: bool = False, checkout: Path | None = None,
        env: str | None = None) -> int:
    """End-to-end single-discharge calibration of the built case.

    Compiles the calibration CSV, ensures the ``.cas`` prints the target,
    emits the HydroBayesCal config, stages the driver and (unless
    ``prepare_only``) launches it. Returns the driver exit code (0 when
    ``prepare_only``).
    """
    from hydromate.calibration import emit_hbc_config

    cfg.ensure_dirs()
    cfg.calibration.calibration_quantities = list(calibration_quantities)
    cfg.calibration.extraction_quantities = list(extraction_quantities)
    print(f"calibration target(s): {list(calibration_quantities)}  "
          f"parameters: {[p.name for p in cfg.calibration.parameters]}")

    csv, table = build_velocity_csv(cfg, vel_err_floor=vel_err_floor,
                                    depth_err_floor=depth_err_floor)
    print(f"calibration points ({len(table)}) -> {csv}")
    if "SCALAR VELOCITY_DATA" in table:
        print(f"  scalar velocity [m/s]: {table['SCALAR VELOCITY_DATA'].min():.3f} .. "
              f"{table['SCALAR VELOCITY_DATA'].max():.3f}")

    ensure_graphic_output(cfg.model_path(cfg.cas_file))
    config_path = emit_hbc_config(cfg, csv)
    print(f"HydroBayesCal config -> {config_path}")

    driver = stage_driver(cfg.calibration_dir, "bal_telemac.py", checkout=checkout)
    if prepare_only:
        print("\n--prepare-only: not launched. Run it with:")
        print(f"  cd {driver.parent} && {sys.executable} {driver.name} "
              f"--config {config_path}")
        return 0
    return launch(cfg, driver, config_path, env=env, note="HydroBayesCal calibration")


# --------------------------------------------------------------------------- #
# multi-flow calibration
# --------------------------------------------------------------------------- #
@dataclass
class FlowSpec:
    """One steady discharge in a multi-flow calibration.

    ``kind`` + ``values`` (+ ``positions``) feed :func:`hydromate.campaigns`; the
    steering file is derived from the built base case, differing only in Q, the
    outflow stage, ``DURATION`` and the results name. ``vel_err_floor`` raises
    the per-point velocity error for a campaign that should inform but not
    dominate the joint likelihood (e.g. a single cross-section).
    """
    name: str                       # short tag (steering + results names)
    discharge: float                # m3/s
    kind: str                       # adapter | transect | inline | csv
    values: Path                    # FlowTracker xlsx (or a ready CSV for kind "csv")
    positions: Path | None = None   # DGPS point layer (adapter/transect)
    sheet: str = "KB15"
    duration: float | None = None   # steady sim seconds (default: cfg duration)
    vel_err_floor: float = campaigns.VELOCITY_ERROR_FLOOR

    def compile(self, crs_epsg: int) -> pd.DataFrame:
        if self.kind == "csv":
            # pre-compiled calibration points (already in the HydroBayesCal
            # schema: id, x, y, z, <QTY>_DATA, <QTY>_ERROR); passed through so
            # case-specific target preparation (e.g. bathymetry-corrected
            # depths, profile-averaged velocities) survives the launcher's
            # recompilation step.
            return pd.read_csv(self.values)
        if self.kind == "adapter":
            return campaigns.compile_adapter(self.values, self.positions, crs_epsg,
                                             self.vel_err_floor, self.name)
        if self.kind == "transect":
            return campaigns.compile_transect(self.values, self.positions,
                                              self.sheet, self.vel_err_floor,
                                              label=self.name)
        if self.kind == "inline":
            return campaigns.compile_inline(self.values, self.sheet,
                                            self.vel_err_floor, label=self.name)
        raise ValueError(f"flow {self.name!r}: unknown kind {self.kind!r}")


def _patch_cas(text: str, key: str, value: str) -> str:
    import re
    new, n = re.subn(rf"^{re.escape(key)}\s*:.*$", f"{key} : {value}", text,
                     count=1, flags=re.M)
    if n != 1:
        raise ValueError(f"keyword {key!r} not found in the base steering file")
    return new


def outflow_stage(cfg: Config, discharge: float, out_dir: Path) -> float:
    """Outflow water-surface elevation at ``discharge`` from the synthetic
    normal-flow rating (extrapolation warning: unreliable far above the geometry
    calibration discharge / overbank)."""
    rating = synthesize_outflow_rating(cfg, discharge,
                                       out=out_dir / f"rating-q{discharge:g}.csv")
    return float(pd.read_csv(rating)["WSE"].iloc[0])


def write_flow_cas(cfg: Config, base_cas: Path, flow: FlowSpec, rating_dir: Path,
                   *, duration: float | None = None, dest_dir: Path | None = None,
                   listing: int | None = None, graphic: int | None = None) -> Path:
    """Derive one flow's steering file from the built base ``.cas``."""
    text = base_cas.read_text()
    wse = outflow_stage(cfg, flow.discharge, rating_dir)
    dur = duration if duration is not None else (flow.duration
                                                 or cfg.hydrodynamics.duration)
    text = _patch_cas(text, "TITLE", f"'{cfg.name} steady {flow.name}'")
    text = _patch_cas(text, "PRESCRIBED FLOWRATES", f"0.;{flow.discharge:.4f}")
    text = _patch_cas(text, "PRESCRIBED ELEVATIONS", f"{wse:.4f};0.")
    text = _patch_cas(text, "DURATION", f"{dur}")
    text = _patch_cas(text, "RESULTS FILE", f"r2d-{flow.name}.slf")
    if listing is not None:
        text = _patch_cas(text, "LISTING PRINTOUT PERIOD", str(listing))
    if graphic is not None:
        text = _patch_cas(text, "GRAPHIC PRINTOUT PERIOD", str(graphic))
    dest = (dest_dir or base_cas.parent) / f"steady2d-{flow.name}.cas"
    dest.write_text(text)
    print(f"  {dest.name}: Q={flow.discharge} m3/s, outflow WSE={wse:.3f} m, "
          f"DURATION={dur} s")
    return dest


def emit_multiflow_config(cfg: Config, out_dir: Path, model_dir: Path,
                          flows: list[FlowSpec], csv_paths: dict[str, Path], *,
                          n_cpus: int, init_runs: int, max_runs: int,
                          complete_bal_mode: bool = True,
                          only_bal_mode: bool = False,
                          prior_samples: int | None = None) -> Path:
    """Write ``config_Telemac_multiflow.py`` (standard blocks + a ``multiflow``
    block of per-flow steering/CSV entries)."""
    c = cfg.calibration
    params = [p.name for p in c.parameters]
    ranges = [[p.min, p.max] for p in c.parameters]
    prior = prior_samples if prior_samples is not None else getattr(
        c, "prior_samples", 5000)
    flow_entries = ",\n".join(
        "        {{'name': {n!r}, 'control_file': {cf!r},\n"
        "         'results_filename_base': {rb!r},\n"
        "         'calibration_pts_file_path': {csv!r}}}".format(
            n=f.name, cf=f"steady2d-{f.name}.cas", rb=f"r2d-{f.name}",
            csv=str(csv_paths[f.name]))
        for f in flows)
    text = f'''"""HydroBayesCal multi-discharge config generated by hydromate for '{cfg.name}'.

Run:  python bal_telemac_multiflow.py --config config_Telemac_multiflow.py
"""

import os

BASE_DIR = {str(model_dir)!r}

paths = {{
    'case_template_dir': BASE_DIR,
    'model_dir': BASE_DIR,
    'res_dir': {str(out_dir)!r},
    'calibration_pts_file_path': {str(csv_paths[flows[0].name])!r},
}}

multiflow = {{
    'flows': [
{flow_entries},
    ],
}}

hydrodynamic_simulation = {{
    'solver_name': 'Telemac2d',
    'n_processors': {n_cpus},
    'results_filename_base': {f"r2d-{flows[0].name}"!r},
    'control_file': {f"steady2d-{flows[0].name}.cas"!r},
    'friction_file': {cfg.friction_tbl!r},
    'fortran_file': None,
}}

morphodynamic_simulation = {{
    'gaia_cas': None,
    'gaia_results_filename_base': None,
}}

calibration = {{
    'parameters': {params!r},
    'param_values': {ranges!r},
    'extraction_quantities': {list(c.extraction_quantities)!r},
    'calibration_quantities': {list(c.calibration_quantities)!r},
    'dict_output_name': 'extraction-data',
}}

sampling = {{
    'init_runs': {init_runs},
    'max_runs': {max_runs},
    'parameter_distribution': 'uniform',
    'parameter_sampling_method': {c.parameter_sampling_method!r},
    'tp_selection_criteria': 'dkl',
    'eval_steps': 1,
    # prior_samples drives a (prior_samples x prior_samples) dense covariance PER
    # location in gpytorch; keep modest (a low-dim roughness posterior needs few).
    'prior_samples': {prior},
    'mc_samples_al': 2000,
    'mc_exploration': 1000,
    'gp_library': {c.gp_library!r},
}}

execution = {{
    'complete_bal_mode': {complete_bal_mode},
    'only_bal_mode': {only_bal_mode},
    'delete_complex_outputs': True,
    'validation': False,
    'user_param_values': False,
}}
'''
    path = Path(out_dir) / "config_Telemac_multiflow.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _restart_ready(multiflow_dir: Path, flows: list[FlowSpec]) -> list[str]:
    """Names of flows whose completed initial design is NOT on disk."""
    return [f.name for f in flows if not (
        multiflow_dir / f"flow-{f.name}" / "auto-saved-results-HydroBayesCal"
        / "restart_data" / "initial-model-outputs.json").exists()]


def compile_flow_csvs(cfg: Config, flows: list[FlowSpec],
                      multiflow_dir: Path) -> dict[str, Path]:
    """Compile each flow's calibration-points CSV; returns {flow name: path}."""
    multiflow_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    print("compiling per-flow calibration points:")
    for f in flows:
        df = f.compile(cfg.crs_epsg)
        out = multiflow_dir / f"measurements-calibration-{f.name}.csv"
        df.to_csv(out, index=False)
        paths[f.name] = out
        print(f"  {f.name} (Q={f.discharge} m3/s): {len(df)} points, "
              f"U {df['SCALAR VELOCITY_DATA'].min():.3f}..{df['SCALAR VELOCITY_DATA'].max():.3f} "
              f"m/s -> {out.name}")
    return paths


def run_multiflow_smoke(cfg: Config, flows: list[FlowSpec], multiflow_dir: Path,
                        csv_paths: dict[str, Path], *, checkout: Path | None = None,
                        env: str | None = None, init_runs: int = 3) -> int:
    """Isolated end-to-end plumbing test: tiny 30 s runs in a copied model dir.

    Verifies steering/friction updates, extraction at every point, per-run JSON
    accumulation (rows must differ across ``init_runs`` distinct ks levels) and
    the combined matrix shape - without the full solver cost.
    """
    smoke = multiflow_dir / "smoke"
    model = smoke / "model"
    if smoke.exists():
        shutil.rmtree(smoke)
    model.mkdir(parents=True)
    src = cfg.model_dir
    for name in ("geometry.slf", "boundaries.cli", "initial-conditions.slf"):
        (model / name).symlink_to(src / name)
    shutil.copy2(src / cfg.friction_tbl, model / cfg.friction_tbl)

    base_cas = src / cfg.cas_file
    print("smoke steering files (DURATION 30 s):")
    for f in flows:
        write_flow_cas(cfg, base_cas, f, smoke, duration=30.0, dest_dir=model,
                       listing=50, graphic=50)
    config_path = emit_multiflow_config(
        cfg, smoke, model, flows, csv_paths, n_cpus=2, init_runs=init_runs,
        max_runs=init_runs, complete_bal_mode=False)
    driver = stage_driver(smoke, "bal_telemac_multiflow.py", checkout=checkout,
                          also=("bal_telemac.py",))
    rc = launch(cfg, driver, config_path, env=env,
                note=f"multiflow SMOKE ({init_runs * len(flows)} tiny runs)")
    if rc != 0:
        return rc
    combined = (smoke / "auto-saved-results-HydroBayesCal" / "calibration-data"
                / "_".join(cfg.calibration.calibration_quantities)
                / "model-results-multiflow.csv")
    if not combined.exists():
        print(f"missing combined results {combined}")
        return 3
    arr = np.loadtxt(combined, delimiter=",", skiprows=1, ndmin=2)
    n_pts = (sum(len(pd.read_csv(p)) for p in csv_paths.values())
             * len(cfg.calibration.calibration_quantities))
    checks = {
        "shape": arr.shape == (init_runs, n_pts),
        "no all-zero run": bool(np.all(arr.any(axis=1))),
        "runs differ (ks varies)":
            len({tuple(np.round(r, 6)) for r in arr}) == init_runs,
    }
    for name, ok in checks.items():
        print(f"  smoke check - {name}: {'OK' if ok else 'FAIL'}")
    return 0 if all(checks.values()) else 3


def run_multiflow_calibration(
        cfg: Config, flows: list[FlowSpec], *, launch_mode: str = "prepare",
        checkout: Path | None = None, env: str | None = None,
        force: bool = False) -> int:
    """Compile CSVs + per-flow steering, emit config, stage drivers, then act by
    ``launch_mode``:

    * ``prepare`` - write everything, do not launch (default);
    * ``smoke``   - run the isolated plumbing test;
    * ``run``     - launch the full calibration (initial design + BAL);
    * ``resume``  - reuse completed per-flow initial designs (only_bal_mode) and
      go straight to surrogate + BAL.
    """
    cfg.ensure_dirs()
    multiflow_dir = cfg.calibration_path("multiflow")
    csv_paths = compile_flow_csvs(cfg, flows, multiflow_dir)

    if launch_mode == "smoke":
        return run_multiflow_smoke(cfg, flows, multiflow_dir, csv_paths,
                                   checkout=checkout, env=env)

    base_cas = cfg.model_path(cfg.cas_file)
    if not base_cas.exists():
        raise SystemExit(f"no built case at {base_cas}; run preprocessing.py first")
    print("per-flow steering files:")
    for f in flows:
        write_flow_cas(cfg, base_cas, f, multiflow_dir)

    resume = launch_mode == "resume"
    if resume:
        missing = _restart_ready(multiflow_dir, flows)
        if missing:
            raise SystemExit(f"resume: no completed initial design for {missing}; "
                             "run the initial design (launch_mode='run') first")

    config_path = emit_multiflow_config(
        cfg, multiflow_dir, cfg.model_dir, flows, csv_paths,
        n_cpus=cfg.telemac.n_processors, init_runs=cfg.calibration.init_runs,
        max_runs=cfg.calibration.max_runs, only_bal_mode=resume)
    print(f"multiflow config -> {config_path}"
          + (" (only_bal_mode: reusing completed initial designs)" if resume else ""))
    driver = stage_driver(multiflow_dir, "bal_telemac_multiflow.py",
                          checkout=checkout, also=("bal_telemac.py",))

    if launch_mode == "prepare":
        print("\nprepared (not launched). Launch with launch_mode='run' "
              "(or 'resume' to reuse completed initial designs).")
        print(f"  cd {driver.parent} && {sys.executable} {driver.name} "
              f"--config {config_path}")
        return 0

    if other_calibration_running(cfg.model_dir) and not force:
        raise SystemExit("another TELEMAC calibration appears active on this case "
                         "(shared friction.tbl); wait or pass force=True")
    note = ("multi-discharge calibration - BAL only (reusing initial designs)"
            if resume else "multi-discharge calibration")
    return launch(cfg, driver, config_path, env=env, note=note)
