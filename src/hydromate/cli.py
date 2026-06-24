"""Command-line entry point: ``hydromate``.

Two forms:

* ``hydromate <config.yml> [--check|--dry-run]`` — build a full TELEMAC case
  (default; the configuration path is positional).
* ``hydromate clip <raster> -b <boundary> -o <out>`` — clip a single raster to a
  region-of-interest polygon, without needing a full configuration.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from hydromate import __version__, pipeline
from hydromate.config import load_config


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hydromate",
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
    p.add_argument("--version", action="version", version=f"hydromate {__version__}")
    return p


def _clip_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hydromate clip",
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


def _run_build(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("hydromate")

    try:
        cfg = load_config(args.config)
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
    for name in ("geometry_slf", "boundary_cli", "friction_tbl", "cas_file",
                 "gaia_cas", "calibration_csv", "hbc_config"):
        value = getattr(art, name)
        if value:
            log.info("  %-16s %s", name, value)
    log.info("next: cd %s && python bal_telemac.py --config %s",
             cfg.model_dir, cfg.hbc_config)
    return 0


def _run_clip(argv: list[str]) -> int:
    args = _clip_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("hydromate")
    from hydromate.dem import clip_to_roi

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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "clip":
        return _run_clip(argv[1:])
    return _run_build(argv)


if __name__ == "__main__":
    sys.exit(main())
