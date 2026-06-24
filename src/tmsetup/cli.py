"""Command-line entry point: ``tm-setup``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tmsetup import __version__, pipeline
from tmsetup.config import load_config


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tm-setup",
        description="Build a calibration-ready TELEMAC-2D/GAIA case from a YAML "
                    "config and emit a HydroBayesCal config.",
    )
    p.add_argument("config", type=Path, help="path to the YAML configuration file")
    p.add_argument("--no-validate-env", action="store_true",
                   help="skip checking that the TELEMAC environment can be sourced")
    p.add_argument("--dry-run", action="store_true",
                   help="launch the solver once after building to validate the case")
    p.add_argument("--check", action="store_true",
                   help="only load and validate the config, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"tmsetup {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    log = logging.getLogger("tmsetup")

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


if __name__ == "__main__":
    sys.exit(main())
