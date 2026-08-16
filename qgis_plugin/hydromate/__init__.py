"""HydroMate - TELEMAC and OpenFOAM river modelling from QGIS.

QGIS calls :func:`classFactory` to load the plugin. Everything else is imported lazily
from inside it, so a broken import in a widget produces a readable error in the plugin
manager rather than a QGIS that will not start.

The one architectural rule this package lives by: **it never imports hydromate.** QGIS
ships its own Python; hydromate needs gmsh, rasterio, geopandas and a solver's
environment. The plugin talks to the ``hydromate`` command-line tool and reads the files
it writes, so either side can be reinstalled without touching the other.
"""

from __future__ import annotations

__version__ = "0.2.0"


def classFactory(iface):        # noqa: N802 - the name QGIS requires
    """Return the plugin instance for *iface*."""
    from .plugin import HydromatePlugin
    return HydromatePlugin(iface)
