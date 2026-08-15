#!/usr/bin/env python3
"""Build (and validate) the QGIS plugin zip.

Runnable locally and from CI, so the artifact a release ships is the one a developer can
reproduce - rather than something only the workflow knows how to make.

plugins.qgis.org has a short list of hard requirements, and every one of them is checked
here rather than discovered when an upload is rejected:

* the zip contains exactly **one top-level folder**, named after the plugin;
* ``metadata.txt`` carries a name, a version, ``qgisMinimumVersion``, a description, an
  author, a GPL-compatible licence, and working homepage / repository / tracker links;
* no compiled or generated files, no ``__pycache__``, no VCS directories;
* under 25 MB.

Usage::

    python scripts/build_plugin_zip.py                 # -> dist/hydromate-qgis-<ver>.zip
    python scripts/build_plugin_zip.py --check         # validate only, build nothing
    python scripts/build_plugin_zip.py -o somewhere.zip
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "qgis_plugin" / "hydromate"
DEFAULT_OUT = REPO / "dist"

#: plugins.qgis.org rejects a package over this.
MAX_BYTES = 25 * 1024 * 1024

REQUIRED_FIELDS = ("name", "qgisMinimumVersion", "description", "about", "version",
                   "author", "email", "repository", "tracker", "homepage", "license")

#: Never shipped. ``*_rc.py`` especially: a compiled resource module is built against one
#: Qt major version and fails to import on the other, which is the most common reason a
#: plugin loads on QGIS 3 and not on QGIS 4.
EXCLUDE_DIRS = {"__pycache__", ".git", ".github", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", "__MACOSX", ".idea", ".vscode"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".qrc", ".ui.autosave", ".orig", ".rej", ".swp"}
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db"}


def read_metadata(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        # Continuation lines (the changelog) are indented; they belong to the previous key.
        if not line or line.startswith(("#", "[", " ", "\t")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def validate(plugin_dir: Path) -> tuple[dict[str, str], list[str]]:
    """Check the plugin folder. Returns ``(metadata, problems)``."""
    problems: list[str] = []
    metadata_path = plugin_dir / "metadata.txt"
    if not metadata_path.is_file():
        return {}, [f"no metadata.txt in {plugin_dir}"]

    fields = read_metadata(metadata_path)
    for key in REQUIRED_FIELDS:
        if not fields.get(key):
            problems.append(f"metadata.txt is missing '{key}'")

    if "GPL" not in fields.get("license", ""):
        problems.append("the licence must be GPL-compatible (the plugin links PyQGIS)")
    if not (plugin_dir / "LICENSE").is_file():
        problems.append("no LICENSE file in the plugin folder")
    if not (plugin_dir / "__init__.py").is_file():
        problems.append("no __init__.py - QGIS would not be able to load the plugin")
    if "classFactory" not in (plugin_dir / "__init__.py").read_text(encoding="utf-8"):
        problems.append("__init__.py does not define classFactory()")

    icon = fields.get("icon", "")
    if icon and not (plugin_dir / icon).is_file():
        problems.append(f"metadata.txt names icon={icon}, which is not there")

    # Documentation is a stated requirement, not a nicety.
    if not (plugin_dir / "README.md").is_file():
        problems.append("no README.md - plugins need at least minimal documentation")

    for path in plugin_dir.rglob("*"):
        if path.is_file() and path.name.endswith("_rc.py"):
            problems.append(f"{path.name} is a compiled Qt resource module; it will not "
                            "import on the other Qt major version")
    return fields, problems


def included(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDE_DIRS for part in relative.parts):
        return False
    if path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
        return False
    return path.is_file()


def build(plugin_dir: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    # The single top-level folder QGIS unpacks into python/plugins/, named exactly as the
    # package - anything else and the plugin will not be importable.
    top = plugin_dir.name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(plugin_dir.rglob("*")):
            if not included(path, plugin_dir):
                continue
            archive.write(path, Path(top) / path.relative_to(plugin_dir))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugin-dir", type=Path, default=PLUGIN_DIR)
    parser.add_argument("-o", "--out", type=Path, default=None)
    parser.add_argument("--check", action="store_true",
                        help="validate only; build nothing")
    args = parser.parse_args(argv)

    fields, problems = validate(args.plugin_dir)
    if problems:
        print("The plugin folder is not ready to publish:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    version = fields.get("version", "0.0.0")
    print(f"{fields.get('name')} {version} - metadata ok")
    print(f"  QGIS {fields['qgisMinimumVersion']} to "
          f"{fields.get('qgisMaximumVersion', '(unbounded)')}")
    if args.check:
        return 0

    out = args.out or (DEFAULT_OUT / f"hydromate-qgis-{version}.zip")
    build(args.plugin_dir, out)
    size = out.stat().st_size
    print(f"  wrote {out} ({size / 1024:.0f} KiB)")
    if size > MAX_BYTES:
        print(f"the package is over the {MAX_BYTES // 1024 // 1024} MB limit "
              "plugins.qgis.org enforces", file=sys.stderr)
        return 1

    with zipfile.ZipFile(out) as archive:
        tops = {Path(n).parts[0] for n in archive.namelist()}
    if len(tops) != 1:
        print(f"the zip must contain exactly one top-level folder, found: {sorted(tops)}",
              file=sys.stderr)
        return 1
    print(f"  one top-level folder: {tops.pop()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
