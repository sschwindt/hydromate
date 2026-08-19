Code documentation
==================

This page is generated automatically from the docstrings in the ``axqua`` package. Each section documents one module. The package is layered: ``axqua.core`` is solver-agnostic, and ``axqua.solvers.<name>`` is one subpackage per simulation code.

Configuration
-------------

.. automodule:: axqua.config
   :members:

Solver-agnostic core
--------------------

``axqua.core`` holds everything that describes the river and the modelling intent rather than a particular simulation code. **Nothing here imports a solver** - backends depend on the core, never the other way round - which is what lets a second simulation code be an addition rather than a rewrite.

.. automodule:: axqua.core.geodata
   :members:

.. automodule:: axqua.core.raster
   :members:

.. automodule:: axqua.core.boundaries
   :members:

.. automodule:: axqua.core.structures
   :members:

.. automodule:: axqua.core.capabilities
   :members:

.. automodule:: axqua.core.registry
   :members:

The OpenFOAM backend
--------------------

.. automodule:: axqua.solvers.openfoam.polymesh
   :members:

.. automodule:: axqua.solvers.openfoam.mesh
   :members:

.. automodule:: axqua.solvers.openfoam.hotstart
   :members:

.. automodule:: axqua.solvers.openfoam.fields
   :members:

.. automodule:: axqua.solvers.openfoam.dicts
   :members:

.. automodule:: axqua.solvers.openfoam.case
   :members:

.. automodule:: axqua.solvers.openfoam.quality
   :members:

.. automodule:: axqua.solvers.openfoam.runtime
   :members:

.. automodule:: axqua.solvers.openfoam.report
   :members:

Compile docs
------------

These docs are built locally with Sphinx; nothing leaves your machine, so this works regardless of whether the repository is public or private. The build needs only Sphinx and the theme (the heavy runtime dependencies are mocked in ``conf.py``); install them into the ``axqua-env`` environment once with:

.. code-block:: bash

   mamba run -n axqua-env pip install -r docs/requirements-docs.txt

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

   mamba run -n axqua-env pip install sphinx-autobuild
   mamba run -n axqua-env sphinx-autobuild docs docs/_build/html

The build output (``docs/_build/``) is git-ignored, so compiling never dirties the repository.

TELEMAC environment bridge
--------------------------

.. automodule:: axqua.env
   :members:

Live solver progress
--------------------

.. automodule:: axqua.progress
   :members:

Stage 1 -- DEM ingest and clipping
----------------------------------

.. automodule:: axqua.dem
   :members:

Stage 2 -- mesh, bathymetry and SELAFIN geometry
------------------------------------------------

.. automodule:: axqua.solvers.telemac.mesh
   :members:

.. automodule:: axqua.core.selafin
   :members:

.. automodule:: axqua.solvers.telemac.mesh_quality
   :members:

Stage 3 -- boundary conditions
------------------------------

.. automodule:: axqua.solvers.telemac.boundary
   :members:

Stage 4 -- steering and friction files
--------------------------------------

.. automodule:: axqua.solvers.telemac.steering
   :members:

Hydraulic input readers
-----------------------

.. automodule:: axqua.hydraulics
   :members:

Stage-discharge rating synthesis
--------------------------------

.. automodule:: axqua.rating
   :members:

Ground-truth ingestion
----------------------

.. automodule:: axqua.ground_truth
   :members:

Calibration-target template
---------------------------

.. automodule:: axqua.targets
   :members:

FlowTracker2 velocity extraction
--------------------------------

.. automodule:: axqua.flowtracker
   :members:

Stage 5 -- calibration CSV and HydroBayesCal config
---------------------------------------------------

.. automodule:: axqua.calibration
   :members:

Pipeline orchestration
----------------------

.. automodule:: axqua.solvers.telemac.pipeline
   :members:

Shared per-case workflow helpers
--------------------------------

.. automodule:: axqua.workflow
   :members:

Boundary-flux convergence (hotstart check)
------------------------------------------

.. automodule:: axqua.solvers.telemac.flux_convergence
   :members:

Mesh-convergence study
----------------------

.. automodule:: axqua.convergence
   :members:

Mesh-resolution validity checks
-------------------------------

.. automodule:: axqua.mesh_validity
   :members:

TELEMAC-3D extension
--------------------

.. automodule:: axqua.solvers.telemac.threed
   :members:

Vertical-layer (3D) convergence study
-------------------------------------

.. automodule:: axqua.vertical_convergence
   :members:

Unsteady (hydrograph) extension
-------------------------------

.. automodule:: axqua.solvers.telemac.unsteady
   :members:

Logging
-------

.. automodule:: axqua.logsetup
   :members:

Command-line interface
----------------------

.. automodule:: axqua.cli
   :members:

The job system
--------------

Job identity, persistence, execution and detachment. A sibling of ``core`` and ``solvers`` rather than part of ``core``, because the executor dispatches to a solver backend - which nothing in ``core`` may do. ``model``, ``ids`` and ``paths`` are standard-library only, so a job verb costs no more than a capability listing.

.. automodule:: axqua.jobs.model
   :members:

.. automodule:: axqua.jobs.ids
   :members:

.. automodule:: axqua.jobs.paths
   :members:

.. automodule:: axqua.jobs.store
   :members:

.. automodule:: axqua.jobs.lock
   :members:

.. automodule:: axqua.jobs.procs
   :members:

.. automodule:: axqua.jobs.index
   :members:

.. automodule:: axqua.jobs.reaper
   :members:

.. automodule:: axqua.jobs.profiles
   :members:

.. automodule:: axqua.jobs.events
   :members:

.. automodule:: axqua.jobs.logs
   :members:

.. automodule:: axqua.jobs.interaction
   :members:

.. automodule:: axqua.jobs.results
   :members:

.. automodule:: axqua.jobs.executor
   :members:

.. automodule:: axqua.jobs.submit
   :members:

Detached launchers
------------------

.. automodule:: axqua.jobs.launcher
   :members:

.. automodule:: axqua.jobs.launchers.systemd
   :members:

.. automodule:: axqua.jobs.launchers.posix
   :members:

.. automodule:: axqua.jobs.launchers.windows
   :members:

.. automodule:: axqua.jobs.launchers.wsl
   :members:

Solver backends
---------------

The adapters the job system dispatches to. Both are thin: every piece of work is a function the per-case scripts already call, so the standalone path and a submitted job drive the same code.

.. automodule:: axqua.solvers.telemac.backend
   :members:

.. automodule:: axqua.solvers.openfoam.backend
   :members:

Job command line
----------------

.. automodule:: axqua.jobcli
   :members:
