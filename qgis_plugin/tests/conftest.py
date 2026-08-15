"""Make the plugin importable alongside the installed ``hydromate`` package.

Both are called ``hydromate`` - the plugin because that is the folder name QGIS installs
and shows, the library because that is its name on PyPI. Inside QGIS they never meet: the
plugin runs in QGIS's Python and the library in the solver's, which is the entire point of
the CLI boundary. In a single pytest process they *would* meet, and the installed package
would win.

So the plugin is loaded here under the alias **``hydromate_plugin``**, from its own
directory. That keeps one ``pytest`` at the repository root able to run both suites, and
it costs nothing at runtime because QGIS imports the real folder by its real name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = PLUGIN_ROOT / "hydromate"
ALIAS = "hydromate_plugin"


def _load_alias() -> None:
    if ALIAS in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        ALIAS, PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)])
    if spec is None or spec.loader is None:         # pragma: no cover
        raise ImportError(f"could not load the plugin package from {PACKAGE_DIR}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so the package's own relative imports resolve against
    # the alias rather than falling back to the installed library.
    sys.modules[ALIAS] = module
    spec.loader.exec_module(module)


_load_alias()
