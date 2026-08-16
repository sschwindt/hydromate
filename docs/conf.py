"""Sphinx configuration for the hydromate documentation (Read the Docs)."""

import os
import sys
from datetime import datetime

# make the package importable for autodoc (sources live in ../src)
sys.path.insert(0, os.path.abspath("../src"))

# -- Project information ------------------------------------------------------
project = "hydromate"
author = "Sebastian Schwindt"
copyright = f"{datetime.now():%Y}, {author}"
release = "0.2.0"
version = "0.1"

# -- General configuration ----------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",      # pull docstrings from the Python modules
    "sphinx.ext.autosummary",  # summary tables
    "sphinx.ext.napoleon",     # parse NumPy-/Google-style docstrings
    "sphinx.ext.viewcode",     # add [source] links
    "sphinx.ext.intersphinx",  # cross-link to numpy/python docs
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Autodoc ------------------------------------------------------------------
autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
# Heavy / optional third-party deps are mocked so the RTD build stays light and
# does not need gmsh, GDAL, etc. installed (docstrings are still rendered).
autodoc_mock_imports = [
    "numpy", "pandas", "scipy", "yaml", "pyproj",
    "shapely", "geopandas", "rasterio", "gmsh",
]

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_use_param = True
napoleon_use_rtype = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
}

# -- HTML output --------------------------------------------------------------
html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = "hydromate"
