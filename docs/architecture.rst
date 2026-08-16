Architecture
============

hydromate is three things that can each be used without the others: a **model builder**,
a **job runner**, and a **QGIS plugin**. This page explains how they fit together and,
more usefully, *why* the seams are where they are.

The chain
---------

.. code-block:: text

    QGIS plugin
        |  submit / monitor / cancel, over the CLI and over files
        v
    hydromate runner  (a detached system job)
        |
        +--> build the case         (mesh, boundaries, steering files)
        +--> run the solver         (TELEMAC-2D/3D + GAIA, or OpenFOAM interFoam)
        +--> postprocess            (flux convergence, reports, optional VTK)
        +--> results/ + results.json
        |
        v
    QGIS plugin loads the result files as styled layers

Three rules
-----------

Everything else follows from these:

.. code-block:: text

    hydromate core MUST NOT depend on QGIS.
    solver code    MUST NOT depend on QGIS.
    QGIS           MUST NOT own solver process lifetime.

The third is the one users feel. A simulation that died when QGIS was closed - or when
QGIS crashed, or when the plugin was reloaded - would be unusable for the multi-day runs
this software exists for. So the plugin **submits** work and never **owns** it.

Why the plugin never imports hydromate
--------------------------------------

QGIS ships its own Python interpreter. hydromate needs gmsh, rasterio, geopandas and a
solver environment. Making those coexist inside the QGIS interpreter would be fragile to
install and would break on every QGIS update.

So the plugin talks to the ``hydromate`` command-line tool as a subprocess, and reads the
files it writes. Both sides can be reinstalled independently, and QGIS's Python never has
to become the solver's Python.

The consequence is that everything crossing that boundary is **small**: a job id, a state,
a path, a capability matrix. Solver field arrays never do - a TELEMAC result is hundreds of
megabytes and a reconstructed OpenFOAM case is gigabytes, so the plugin is handed paths and
QGIS opens the files itself.

Package layout
--------------

``hydromate.core``
    Solver-agnostic: configuration, geodata, rasters, boundaries, structures, the SERAFIN
    codec, capabilities, the solver registry, typed errors, environment handling.
    **Nothing here imports a solver**, which is checked by a test that reads the source.

``hydromate.solvers.telemac`` / ``hydromate.solvers.openfoam``
    One subpackage per code. **Neither may reach into the other**; orchestration that
    drives both (obtaining a TELEMAC seed for an OpenFOAM build, say) lives above them.

``hydromate.jobs``
    Job identity, persistence, execution and detachment. A sibling of the other two rather
    than part of ``core``, because the executor dispatches to a backend.

``qgis_plugin/hydromate``
    The QGIS plugin. GPLv2+ (it links PyQGIS); the library stays BSD-3-Clause.

Solvers are discovered through the ``hydromate.solvers`` entry-point group, so adding a
third is ``pip install hydromate-<name>`` rather than a fork.

Capabilities, and why the plugin has no hardcoded tabs
------------------------------------------------------

Every case reports, per solver, what it can do - on three axes that are deliberately kept
apart because each has a different fix:

``implemented``
    Does hydromate support this **for this solver**? Three values, not two: OpenFOAM's
    ``steady2d`` is *not applicable* (a VOF free-surface model is inherently 3D and
    transient), while its ``morphodynamics`` is *not implemented* (it could be; it is
    not). Reporting "no" for both would mislead.

``configured``
    Does **this case** ask for it? From the configuration alone.

``built`` / ``run``
    Do the artifacts exist? A path check, so it stays cheap.

``hydromate case-status <config> --json`` publishes that matrix, and the plugin builds its
tab set from it: ``n/a`` hides a tab, ``no`` shows it disabled *with the reason*, and the
buttons enable from ``configured``/``built``/``run``. Adding a capability to hydromate
therefore appears in the plugin with no plugin change.

Jobs
----

See :doc:`jobs` for the full model. The essentials:

* a job is a **directory**, and that directory is authoritative;
* ``job.json`` freezes the resolved configuration at submit time, so editing the case
  afterwards cannot change a running job and a finished job is reproducible from its own
  folder;
* ``status.json`` is written only by the runner, replaced atomically, and read without
  locking;
* the SQLite index is a **cache** that can be rebuilt by scanning, and every index failure
  is ignored - a solver failure must never corrupt the registry.

Standalone use is not a fallback
--------------------------------

The per-case scripts (``preprocessing.py``, ``initial_run.py``, …) and
``hydromate execute job.json`` remain fully supported with QGIS absent, and they call the
**same** orchestration functions the job system does - the backends are adapters over them,
not reimplementations. That is what keeps the two paths from drifting.
