Code documentation
==================

This page is generated automatically from the docstrings in the ``hydromate`` package. Each section documents one module. The package is layered: ``hydromate.core`` is solver-agnostic, and ``hydromate.solvers.<name>`` is one subpackage per simulation code.

Configuration
-------------------------------------------------------------------------------

.. automodule:: hydromate.config
   :members:

Solver-agnostic core
-------------------------------------------------------------------------------

``hydromate.core`` holds everything that describes the river and the modelling intent
rather than a particular simulation code. **Nothing here imports a solver** - backends
depend on the core, never the other way round - which is what lets a second simulation
code be an addition rather than a rewrite.

.. automodule:: hydromate.core.geodata
   :members:

.. automodule:: hydromate.core.raster
   :members:

.. automodule:: hydromate.core.boundaries
   :members:

.. automodule:: hydromate.core.structures
   :members:

.. automodule:: hydromate.core.capabilities
   :members:

.. automodule:: hydromate.core.registry
   :members:

The OpenFOAM backend
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.openfoam.polymesh
   :members:

.. automodule:: hydromate.solvers.openfoam.mesh
   :members:

.. automodule:: hydromate.solvers.openfoam.hotstart
   :members:

.. automodule:: hydromate.solvers.openfoam.fields
   :members:

.. automodule:: hydromate.solvers.openfoam.dicts
   :members:

.. automodule:: hydromate.solvers.openfoam.case
   :members:

.. automodule:: hydromate.solvers.openfoam.quality
   :members:

.. automodule:: hydromate.solvers.openfoam.runtime
   :members:

.. automodule:: hydromate.solvers.openfoam.report
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

.. automodule:: hydromate.solvers.telemac.mesh
   :members:

.. automodule:: hydromate.core.selafin
   :members:

.. automodule:: hydromate.solvers.telemac.mesh_quality
   :members:

Stage 3 -- boundary conditions
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.telemac.boundary
   :members:

Stage 4 -- steering and friction files
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.telemac.steering
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

Calibration-target template
-------------------------------------------------------------------------------

.. automodule:: hydromate.targets
   :members:

FlowTracker2 velocity extraction
-------------------------------------------------------------------------------

.. automodule:: hydromate.flowtracker
   :members:

Stage 5 -- calibration CSV and HydroBayesCal config
-------------------------------------------------------------------------------

.. automodule:: hydromate.calibration
   :members:

Pipeline orchestration
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.telemac.pipeline
   :members:

Shared per-case workflow helpers
-------------------------------------------------------------------------------

.. automodule:: hydromate.workflow
   :members:

Boundary-flux convergence (hotstart check)
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.telemac.flux_convergence
   :members:

Mesh-convergence study
-------------------------------------------------------------------------------

.. automodule:: hydromate.convergence
   :members:

Mesh-resolution validity checks
-------------------------------------------------------------------------------

.. automodule:: hydromate.mesh_validity
   :members:

TELEMAC-3D extension
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.telemac.threed
   :members:

Vertical-layer (3D) convergence study
-------------------------------------------------------------------------------

.. automodule:: hydromate.vertical_convergence
   :members:

Unsteady (hydrograph) extension
-------------------------------------------------------------------------------

.. automodule:: hydromate.solvers.telemac.unsteady
   :members:

Logging
-------------------------------------------------------------------------------

.. automodule:: hydromate.logsetup
   :members:

Command-line interface
-------------------------------------------------------------------------------

.. automodule:: hydromate.cli
   :members:
