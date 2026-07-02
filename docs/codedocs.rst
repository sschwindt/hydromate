Code documentation
==================

This page is generated automatically from the docstrings in the ``hydromate`` package. Each section documents one module of the pipeline.

Configuration
-------------------------------------------------------------------------------

.. automodule:: hydromate.config
   :members:

Compile docs
-------------------------------------------------------------------------------

These docs are built locally with Sphinx; nothing leaves your machine, so this works regardless of whether the repository is public or private. The build needs only Sphinx and the theme (the heavy runtime dependencies are mocked in ``conf.py``); install them into the ``hydromate-env`` environment once with:

.. code-block:: bash

   mamba run -n hydromate-env pip install -r docs/requirements-docs.txt

Build the HTML site with the provided Makefile (run from the ``docs/`` directory) and open it in your browser:

.. code-block:: bash

   cd docs
   make html
   xdg-open _build/html/index.html

The Makefile is only a convenience wrapper; the equivalent direct call (run from the repository root) is:

.. code-block:: bash

   sphinx-build -b html docs docs/_build/html

While editing the documentation, ``sphinx-autobuild`` rebuilds and refreshes the browser on every save, serving the site at ``http://127.0.0.1:8000``:

.. code-block:: bash

   mamba run -n hydromate-env pip install sphinx-autobuild
   mamba run -n hydromate-env sphinx-autobuild docs docs/_build/html

The build output (``docs/_build/``) is git-ignored, so compiling never dirties the repository.

TELEMAC environment bridge
-------------------------------------------------------------------------------

.. automodule:: hydromate.env
   :members:

Live solver progress
-------------------------------------------------------------------------------

.. automodule:: hydromate.progress
   :members:

Stage 1 -- DEM ingest and clipping
-------------------------------------------------------------------------------

.. automodule:: hydromate.dem
   :members:

Stage 2 -- mesh, bathymetry and SELAFIN geometry
-------------------------------------------------------------------------------

.. automodule:: hydromate.mesh
   :members:

.. automodule:: hydromate.selafin
   :members:

.. automodule:: hydromate.mesh_quality
   :members:

Stage 3 -- boundary conditions
-------------------------------------------------------------------------------

.. automodule:: hydromate.boundary
   :members:

Stage 4 -- steering and friction files
-------------------------------------------------------------------------------

.. automodule:: hydromate.steering
   :members:

Hydraulic input readers
-------------------------------------------------------------------------------

.. automodule:: hydromate.hydraulics
   :members:

Stage-discharge rating synthesis
-------------------------------------------------------------------------------

.. automodule:: hydromate.rating
   :members:

Ground-truth ingestion
-------------------------------------------------------------------------------

.. automodule:: hydromate.ground_truth
   :members:

Stage 5 -- calibration CSV and HydroBayesCal config
-------------------------------------------------------------------------------

.. automodule:: hydromate.calibration
   :members:

Pipeline orchestration
-------------------------------------------------------------------------------

.. automodule:: hydromate.pipeline
   :members:

Shared per-case workflow helpers
-------------------------------------------------------------------------------

.. automodule:: hydromate.workflow
   :members:

Boundary-flux convergence (hotstart check)
-------------------------------------------------------------------------------

.. automodule:: hydromate.flux_convergence
   :members:

Mesh-convergence study
-------------------------------------------------------------------------------

.. automodule:: hydromate.convergence
   :members:

TELEMAC-3D extension
-------------------------------------------------------------------------------

.. automodule:: hydromate.threed
   :members:

Vertical-layer (3D) convergence study
-------------------------------------------------------------------------------

.. automodule:: hydromate.vertical_convergence
   :members:

Unsteady (hydrograph) extension
-------------------------------------------------------------------------------

.. automodule:: hydromate.unsteady
   :members:

Logging
-------------------------------------------------------------------------------

.. automodule:: hydromate.logsetup
   :members:

Command-line interface
-------------------------------------------------------------------------------

.. automodule:: hydromate.cli
   :members:
