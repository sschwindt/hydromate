"""Command-line entry point: ``axqua``.

Two forms:

* ``axqua <config.yml> [--check|--dry-run]`` - build a full TELEMAC case
  (default; the configuration path is positional).
* ``axqua clip <raster> -b <boundary> -o <out>`` - clip a single raster to a
  region-of-interest polygon, without needing a full configuration.
* ``axqua rating -o <out.csv> --manning <n>|--strickler <Kst> --slope <S0>
  --width <b> [--side-slope <m>] [--bed-elevation <z>] --q <Q...>`` - synthesise a
  normal-flow outflow stage-discharge curve to use as ``boundaries.stage_discharge``.
* ``axqua targets <config.yml> [-o <out.xlsx>] [--force]`` - generate the
  user-fillable ``calibration-target-data.xlsx`` template (hydraulics /
  morphodynamics ground truth + calibration-parameter ranges) for a case.
* ``axqua openfoam <config.yml> [--check] [--cell-size <m>] [--layers <n>]`` -
  build the OpenFOAM (interFoam) free-surface case: a terrain-following
  all-hexahedral mesh, fields seeded from the converged TELEMAC 2D result, and both
  staged dictionary sets. ``--check`` only reports the cell count.
* ``axqua case-status <config.yml> [--check-env] [--full] [--json]`` - report what
  the case can do (implemented / configured / built / run, per solver) and refresh its
  ``MODEL=<SOLVER>_<ENABLED|DISABLED>`` marker files. ``--json`` is what a QGIS plugin
  reads to decide which tabs to show.

Job verbs (see :mod:`axqua.jobcli`), which run work that outlives this shell:

* ``axqua submit <config.yml> --kind <kind>`` - create a persistent job and start
  it detached; prints the job id.
* ``axqua execute <job>`` - run a job synchronously here (the debugging path).
* ``axqua status <JOB_ID>`` / ``cancel`` / ``logs`` / ``list`` / ``profiles``.

``status`` is overloaded, and the argument decides: an existing **path** means the case
(the historic meaning, kept working), while an argument matching the job-id grammar means
the job. The two cannot collide - a job id is never a path.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from axqua import __version__, pipeline
from axqua.config import load_config
from axqua.logsetup import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua",
        description="Build a calibration-ready TELEMAC-2D/GAIA case from a YAML "
                    "config and emit a HydroBayesCal config. Use the 'clip' "
                    "subcommand to only crop a raster to a ROI.",
    )
    p.add_argument("config", type=Path, help="path to the YAML configuration file")
    p.add_argument("--no-validate-env", action="store_true",
                   help="skip checking that the TELEMAC environment can be sourced")
    p.add_argument("--dry-run", action="store_true",
                   help="launch the solver once after building to validate the case")
    p.add_argument("--check", action="store_true",
                   help="only load and validate the config, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"axqua {__version__}")
    return p


def _clip_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua clip",
        description="Clip a raster to a region-of-interest polygon. The raster is "
                    "cropped in its own grid/CRS by default; if it has no CRS the "
                    "boundary's CRS is assumed and stamped onto the output.",
    )
    p.add_argument("raster", type=Path, help="input raster (GeoTIFF)")
    p.add_argument("-b", "--boundary", type=Path, required=True,
                   help="ROI polygon (shapefile/GeoPackage/...)")
    p.add_argument("-o", "--out", type=Path, required=True, help="output raster path")
    p.add_argument("--epsg", type=int, default=None,
                   help="reproject the raster to this EPSG before clipping "
                        "(default: keep the source CRS)")
    p.add_argument("--all-touched", action="store_true",
                   help="keep pixels touched by the polygon edge (default: centre-in)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _rating_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua rating",
        description="Generate a normal-flow (uniform Manning/Strickler) outflow "
                    "stage-discharge curve for a prismatic trapezoidal channel and "
                    "write it as a Q,WSE,depth CSV to use as boundaries.stage_discharge.",
    )
    p.add_argument("-o", "--out", type=Path, required=True, help="output CSV path")
    rough = p.add_mutually_exclusive_group(required=True)
    rough.add_argument("--manning", type=float, help="Manning's n (e.g. 0.03)")
    rough.add_argument("--strickler", type=float, help="Strickler Kst = 1/n (e.g. 33)")
    p.add_argument("--slope", type=float, required=True,
                   help="longitudinal bed slope S0 (m/m, > 0)")
    p.add_argument("--width", type=float, required=True,
                   help="channel bottom width b (m)")
    p.add_argument("--side-slope", type=float, default=0.0,
                   help="bank side slope m = horizontal:vertical (0 = rectangular)")
    p.add_argument("--bed-elevation", type=float, default=0.0,
                   help="outflow bed elevation z (m a.s.l.); WSE = z + normal depth")
    p.add_argument("--q", type=float, nargs="+", required=True,
                   help="discharge(s) m3/s; one (the steady Q) is enough for a steady run")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _run_rating(argv: list[str]) -> int:
    args = _rating_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("axqua")
    from axqua.rating import generate_stage_discharge

    try:
        out = generate_stage_discharge(
            args.out, args.q, manning=args.manning, strickler=args.strickler,
            slope=args.slope, bottom_width=args.width, side_slope=args.side_slope,
            bed_elevation=args.bed_elevation,
        )
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1
    log.info("wrote stage-discharge rating -> %s", out)
    return 0


def _targets_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua targets",
        description="Generate the calibration-target-data.xlsx template for a case: "
                    "a hydraulics tab (velocities, fluctuations, TKE, depth, bed "
                    "elevation), a morphodynamics tab (grain sizes + DoD targets) - "
                    "both keyed by unique IDs joining a point layer in "
                    "user-sources/geodata/ - and a parameters tab with drop-down "
                    "calibration parameters, min/max test ranges and range tips.",
    )
    p.add_argument("config", type=Path, help="path to the YAML configuration file")
    p.add_argument("-o", "--out", type=Path, default=None,
                   help="output .xlsx (default: ground_truth.targets.file from the "
                        "config, else user-sources/ground-truth/calibration-target-data.xlsx)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing (possibly filled-in) template")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _run_targets(argv: list[str]) -> int:
    args = _targets_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("axqua")
    from axqua.targets import write_target_template

    try:
        cfg = load_config(args.config)
        out = write_target_template(cfg, path=args.out, force=args.force)
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1
    log.info("fill in %s, reference it under ground_truth.targets in %s, then "
             "re-run the build to refresh the calibration CSV + HydroBayesCal config",
             out, args.config)
    return 0


def _status_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua status",
        description="Report what this case can do, and write its "
                    "MODEL=<SOLVER>_<ENABLED|DISABLED> marker files. Each capability "
                    "is reported on three axes: implemented (does axqua support "
                    "it for that solver), configured (does this case ask for it), and "
                    "built/run (do the artifacts exist).",
    )
    p.add_argument("config", type=Path, help="path to the YAML configuration file")
    p.add_argument("--check-env", action="store_true",
                   help="also probe whether each solver's environment can be sourced "
                        "on THIS machine (spawns a shell per solver)")
    p.add_argument("--full", action="store_true",
                   help="print the full marker tables instead of the short summary")
    p.add_argument("--no-write", action="store_true",
                   help="report only; do not write the marker files")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="emit the capability matrix as JSON - what a QGIS plugin reads "
                        "to decide which tabs to show")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _run_status(argv: list[str]) -> int:
    args = _status_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    from axqua import jobcli
    from axqua.core.capabilities import CaseStatus

    if args.as_json:
        # Narration to stderr so stdout carries the document alone.
        jobcli._setup_logging(args)
    try:
        cfg = load_config(args.config)
        status = CaseStatus.collect(cfg, check_env=args.check_env)
    except Exception as exc:
        return jobcli.fail("case-status", exc, as_json=args.as_json,
                           verbose=args.verbose)

    written = [] if args.no_write else [str(p) for p in status.write_markers()]
    if args.as_json:
        payload = status.as_dict()
        payload["markers"] = written
        return jobcli.emit("case-status", payload, as_json=True)
    print(status.render() if args.full else "\n".join(status.summary()))
    for path in written:
        print(f"wrote {Path(path).name}")
    return 0


def _openfoam_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua openfoam",
        description="Build the OpenFOAM (interFoam) free-surface case for a reach: a "
                    "terrain-following all-hexahedral mesh written straight to "
                    "constant/polyMesh (no snappyHexMesh), fields seeded from the "
                    "converged TELEMAC 2D result, and the two staged dictionary sets "
                    "for the spin-up and production runs.",
    )
    p.add_argument("config", type=Path, help="path to the YAML configuration file")
    p.add_argument("--check", action="store_true",
                   help="report the cell count the current settings would produce, "
                        "then exit without building")
    p.add_argument("--hotstart", type=Path, default=None,
                   help="2D result to seed from (default: the case's own r2d.slf)")
    p.add_argument("--pre-run", dest="pre_run", action="store_true", default=None,
                   help="run TELEMAC first to produce the seed (the default; see "
                        "openfoam.pre_run)")
    p.add_argument("--no-pre-run", dest="pre_run", action="store_false",
                   help="never start TELEMAC: seed from an existing r2d.slf if there "
                        "is one, else build cold under a flat lid")
    p.add_argument("--cell-size", type=float, default=None,
                   help="override openfoam.cell_size [m] for this build")
    p.add_argument("--layers", type=int, default=None,
                   help="override openfoam.n_layers for this build")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _run_openfoam(argv: list[str]) -> int:
    args = _openfoam_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("axqua")
    from axqua.solvers.openfoam import build_case, estimate_cells, load_hotstart, summarise

    try:
        cfg = load_config(args.config)
        if args.cell_size is not None:
            cfg.openfoam.cell_size = args.cell_size
        if args.layers is not None:
            cfg.openfoam.n_layers = args.layers
        # An explicit --hotstart is the user naming the seed, so no pre-run is run
        # or needed; otherwise find or produce one.
        seed = args.hotstart
        if seed is None and not args.check:
            from axqua.prerun import ensure_seed

            result = ensure_seed(cfg, enabled=args.pre_run)
            for line in result.summary():
                log.info("%s", line)
            seed = result.path
        state = load_hotstart(cfg, seed)
        if args.check:
            n = estimate_cells(cfg, state=state)
            log.info("a %g m lattice with %d layers gives about %s cells",
                     cfg.openfoam.cell_size, cfg.openfoam.n_layers, f"{n:,}")
            return 0
        art = build_case(cfg, state=state)
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1
    for line in summarise(art):
        log.info("%s", line)
    log.info("run it with  python cases/<your-case>/openfoam_run.py")
    return 0


def _run_build(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=level)            # console now; compound logfile once config loads
    log = logging.getLogger("axqua")

    try:
        cfg = load_config(args.config)
        setup_logging(cfg.model_path(cfg.log_file), level=level)  # -> <model_dir>/axqua.log
        if args.check:
            cfg.validate()
            log.info("config OK: case '%s', model_dir=%s", cfg.name, cfg.model_dir)
            return 0
        art = pipeline.run(
            cfg, validate_env=not args.no_validate_env, dry_run=args.dry_run
        )
    except Exception as exc:  # surface a clean message, full trace in verbose mode
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1

    log.info("done. Produced case in %s", cfg.model_dir)
    for name in ("geometry_slf", "boundary_cli", "friction_tbl", "initial_conditions",
                 "cas_file", "unsteady_cas", "liquid_boundaries", "gaia_cas",
                 "ground_truth", "calibration_csv", "hbc_config"):
        value = getattr(art, name)
        if value:
            log.info("  %-16s %s", name, value)
    log.info("next: cd %s && python bal_telemac.py --config %s",
             cfg.model_dir, cfg.hbc_config)
    return 0


def _run_clip(argv: list[str]) -> int:
    args = _clip_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("axqua")
    from axqua.dem import clip_to_roi

    try:
        out = clip_to_roi(
            args.raster, args.boundary, args.out,
            target_epsg=args.epsg, all_touched=args.all_touched,
        )
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1
    log.info("clipped %s -> %s", args.raster, out)
    return 0


def _migrate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua migrate",
        description="Rewrite a case config in the current schema. Loading a config "
                    "already applies every compatibility mapping (a `percolation:` "
                    "block becomes `gain_lose:`, `work_dir` becomes "
                    "`preprocessing_dir`, and so on); this writes the result back, so "
                    "the deprecation warnings stop and the file says what axqua "
                    "actually read. NOTE that comments are not preserved - the config "
                    "is written from the loaded schema, so a comment cannot survive an "
                    "edit it no longer describes. Keep the annotated original if you "
                    "want the prose (cases/case-template/case-config.yml has it all).",
    )
    p.add_argument("config", type=Path, help="path to the YAML configuration file")
    p.add_argument("-o", "--out", type=Path, default=None,
                   help="write here (default: print to stdout)")
    p.add_argument("--in-place", action="store_true",
                   help="overwrite the config, keeping a .bak copy beside it")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _run_migrate(argv: list[str]) -> int:
    args = _migrate_parser().parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("axqua")
    from axqua.config import dump_config

    try:
        cfg = load_config(args.config)
        # Written relative to where the file will LIVE, not where it came from, so
        # `-o elsewhere/case.yml` does not silently break every relative data path.
        target = args.config if args.in_place else args.out
        base = Path(target).resolve().parent if target else Path(cfg.config_dir)
        text = dump_config(cfg, base=base)
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1

    if args.in_place:
        backup = args.config.with_suffix(args.config.suffix + ".bak")
        shutil.copy2(args.config, backup)
        args.config.write_text(text)
        log.info("migrated %s (original kept as %s)", args.config, backup.name)
    elif args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        log.info("wrote %s", args.out)
    else:
        print(text, end="")
    return 0

def _job(name: str):
    """Late-bind a job verb.

    Imported on call rather than at module scope so ``axqua --check`` on a case does
    not pay for the job machinery, and so a broken job module could never stop the build
    commands from working.
    """
    def run(argv: list[str]) -> int:
        from axqua import jobcli
        return getattr(jobcli, name)(argv)
    return run


#: The whole command surface, as a table. A verb is one entry here plus its ``_run_*``;
#: the alternative - ``argparse`` subparsers - cannot express the default form
#: (``axqua <config.yml>``, with no verb at all) without a pre-pass on ``argv[0]``,
#: so the chain would survive the conversion anyway and only the help output would churn.
_DISPATCH = {
    "clip": lambda argv: _run_clip(argv),
    "rating": lambda argv: _run_rating(argv),
    "migrate": lambda argv: _run_migrate(argv),
    "targets": lambda argv: _run_targets(argv),
    "openfoam": lambda argv: _run_openfoam(argv),
    "case-status": lambda argv: _run_status(argv),
    "submit": _job("run_submit"),
    "execute": _job("run_execute"),
    "cancel": _job("run_cancel"),
    "logs": _job("run_logs"),
    "list": _job("run_list"),
    "profiles": _job("run_profiles"),
}


def _dispatch_status(argv: list[str]) -> int:
    """``status`` means the case or the job, decided by the argument.

    An existing path is a case configuration - the historic meaning, which keeps every
    script and doc snippet written against it working, with a notice pointing at the new
    spelling. Anything matching the job-id grammar is a job. Neither can be mistaken for
    the other, so there is no guess here, only a rule.
    """
    positional = [a for a in argv if not a.startswith("-")]
    first = positional[0] if positional else ""
    if first and Path(first).exists():
        logging.getLogger("axqua").warning(
            "'axqua status <config>' now also accepts a job id; the case form is "
            "spelled 'axqua case-status <config>'. Both work.")
        return _run_status(argv)
    from axqua import jobcli
    return jobcli.run_job_status(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        if argv[0] == "status":
            return _dispatch_status(argv[1:])
        handler = _DISPATCH.get(argv[0])
        if handler is not None:
            return handler(argv[1:])
    return _run_build(argv)


if __name__ == "__main__":
    sys.exit(main())
