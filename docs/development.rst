Development
===========

For contributors and for anyone driving ``axqua`` from Python rather than from the QGIS plugin or the command line. The first half is the architecture - what the pieces are and *why* the seams are where they are - and the second half is the API reference generated from the docstrings.

Architecture
------------

aXqua is three things that can each be used without the others: a **model builder**, a **job runner**, and a **QGIS plugin**. Here is how they fit together and, more usefully, *why* the seams are where they are.

The chain
~~~~~~~~~

.. code-block:: text

    QGIS plugin
        |  submit / monitor / cancel, over the CLI and over files
        v
    axqua runner  (a detached system job)
        |
        +--> build the case         (mesh, boundaries, steering files)
        +--> run the solver         (TELEMAC-2D/3D + GAIA, or OpenFOAM interFoam)
        +--> postprocess            (flux convergence, reports, optional VTK)
        +--> results/ + results.json
        |
        v
    QGIS plugin loads the result files as styled layers

Three rules
~~~~~~~~~~~

Everything else follows from these:

.. code-block:: text

    axqua core MUST NOT depend on QGIS.
    solver code    MUST NOT depend on QGIS.
    QGIS           MUST NOT own solver process lifetime.

The third is the one users feel. A simulation that died when QGIS was closed - or when QGIS crashed, or when the plugin was reloaded - would be unusable for the multi-day runs this software exists for. So the plugin **submits** work and never **owns** it.

Why the plugin never imports aXqua
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

QGIS ships its own Python interpreter. aXqua needs gmsh, rasterio, geopandas and a solver environment. Making those coexist inside the QGIS interpreter would be fragile to install and would break on every QGIS update.

So the plugin talks to the ``axqua`` command-line tool as a subprocess, and reads the files it writes. Both sides can be reinstalled independently, and QGIS's Python never has to become the solver's Python.

The consequence is that everything crossing that boundary is **small**: a job id, a state, a path, a capability matrix. Solver field arrays never do - a TELEMAC result is hundreds of megabytes and a reconstructed OpenFOAM case is gigabytes, so the plugin is handed paths and QGIS opens the files itself.

Package layout
~~~~~~~~~~~~~~

``axqua.core``
    Solver-agnostic: configuration, geodata, rasters, boundaries, structures, the SERAFIN codec, capabilities, the solver registry, typed errors, environment handling. **Nothing here imports a solver**, which is checked by a test that reads the source.

``axqua.solvers.telemac`` / ``axqua.solvers.openfoam``
    One subpackage per code. **Neither may reach into the other**; orchestration that drives both (obtaining a TELEMAC seed for an OpenFOAM build, say) lives above them.

``axqua.jobs``
    Job identity, persistence, execution and detachment. A sibling of the other two rather than part of ``core``, because the executor dispatches to a backend.

``qgis_plugin/axqua``
    The QGIS plugin. GPLv2+ (it links PyQGIS); the library stays BSD-3-Clause.

Solvers are discovered through the ``axqua.solvers`` entry-point group, so adding a third is ``pip install axqua-<name>`` rather than a fork.

Capabilities, and why the plugin has no hardcoded tabs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every case reports, per solver, what it can do - on three axes that are deliberately kept apart because each has a different fix:

``implemented``
    Does aXqua support this **for this solver**? Three values, not two: OpenFOAM's ``steady2d`` is *not applicable* (a VOF free-surface model is inherently 3D and transient), while its ``morphodynamics`` is *not implemented* (it could be; it is not). Reporting "no" for both would mislead.

``configured``
    Does **this case** ask for it? From the configuration alone.

``built`` / ``run``
    Do the artifacts exist? A path check, so it stays cheap.

``axqua case-status <config> --json`` publishes that matrix, and the plugin builds its tab set from it: ``n/a`` hides a tab, ``no`` shows it disabled *with the reason*, and the buttons enable from ``configured``/``built``/``run``. Adding a capability to aXqua therefore appears in the plugin with no plugin change.

Jobs
~~~~

See :doc:`advanced` for the full model. The essentials:

* a job is a **directory**, and that directory is authoritative;
* ``job.json`` freezes the resolved configuration at submit time, so editing the case afterwards cannot change a running job and a finished job is reproducible from its own folder;
* ``status.json`` is written only by the runner, replaced atomically, and read without locking;
* the SQLite index is a **cache** that can be rebuilt by scanning, and every index failure is ignored - a solver failure must never corrupt the registry.

Standalone use is not a fallback
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The per-case scripts (``preprocessing.py``, ``initial_run.py``, …) and ``axqua execute job.json`` remain fully supported with QGIS absent, and they call the **same** orchestration functions the job system does - the backends are adapters over them, not reimplementations. That is what keeps the two paths from drifting.


Code reference
--------------

This page is generated automatically from the docstrings in the ``axqua`` package. Each section documents one module. The package is layered: ``axqua.core`` is solver-agnostic, and ``axqua.solvers.<name>`` is one subpackage per simulation code.

Configuration
~~~~~~~~~~~~~

.. automodule:: axqua.config
   :members:

Solver-agnostic core
~~~~~~~~~~~~~~~~~~~~

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
~~~~~~~~~~~~~~~~~~~~

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
~~~~~~~~~~~~

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
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.env
   :members:

Live solver progress
~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.progress
   :members:

Stage 1 -- DEM ingest and clipping
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.dem
   :members:

Stage 2 -- mesh, bathymetry and SELAFIN geometry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.mesh
   :members:

.. automodule:: axqua.core.selafin
   :members:

.. automodule:: axqua.solvers.telemac.mesh_quality
   :members:

Stage 3 -- boundary conditions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.boundary
   :members:

Stage 4 -- steering and friction files
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.steering
   :members:

Hydraulic input readers
~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.hydraulics
   :members:

Stage-discharge rating synthesis
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.rating
   :members:

Ground-truth ingestion
~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.ground_truth
   :members:

Calibration-target template
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.targets
   :members:

FlowTracker2 velocity extraction
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.flowtracker
   :members:

Stage 5 -- calibration CSV and HydroBayesCal config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.calibration
   :members:

Pipeline orchestration
~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.pipeline
   :members:

Shared per-case workflow helpers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.workflow
   :members:

Boundary-flux convergence (hotstart check)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.flux_convergence
   :members:

Mesh-convergence study
~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.convergence
   :members:

Mesh-resolution validity checks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.mesh_validity
   :members:

TELEMAC-3D extension
~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.threed
   :members:

Vertical-layer (3D) convergence study
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.vertical_convergence
   :members:

Unsteady (hydrograph) extension
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.solvers.telemac.unsteady
   :members:

Logging
~~~~~~~

.. automodule:: axqua.logsetup
   :members:

Command-line interface
~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: axqua.cli
   :members:

The job system
~~~~~~~~~~~~~~

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
~~~~~~~~~~~~~~~~~~

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
~~~~~~~~~~~~~~~

The adapters the job system dispatches to. Both are thin: every piece of work is a function the per-case scripts already call, so the standalone path and a submitted job drive the same code.

.. automodule:: axqua.solvers.telemac.backend
   :members:

.. automodule:: axqua.solvers.openfoam.backend
   :members:

Job command line
~~~~~~~~~~~~~~~~

.. automodule:: axqua.jobcli
   :members:
