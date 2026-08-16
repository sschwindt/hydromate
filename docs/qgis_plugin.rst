The QGIS plugin
===============

**HydroMate** puts the whole workflow in QGIS: choose a case, submit a run, watch it, and
load the results as styled layers - with the simulation itself running outside QGIS, so
closing QGIS does not stop it.

.. figure:: img/inflow-bc.jpg
   :alt: an inflow boundary drawn in QGIS
   :align: center

   Boundaries, mesh zones and ground-truth points are drawn in QGIS anyway. The plugin
   closes the loop.

Installing
----------

The plugin is a **frontend**. It runs no solver and does no heavy numerics; it drives the
separately installed ``hydromate`` command-line tool.

1. **Install hydromate** in the environment that already reaches your solver::

       pip install hydromate
       hydromate --version

2. **Install the plugin.** From the QGIS plugin repository (*Plugins ▸ Manage and Install
   Plugins*, tick *Show also experimental plugins*), or from the zip attached to a
   `GitHub release <https://github.com/sschwindt/hydromate/releases>`_ via *Install from
   ZIP*.

   For development, symlink the folder into your QGIS profile instead::

       ln -s /path/to/hydromate/qgis_plugin/hydromate \
             ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/hydromate

3. **Point the plugin at hydromate.** Open the HydroMate panel and press *Check* on the
   Setup tab. If it is not on ``PATH``, set the path in *Settings…* - this is validated
   immediately, so a wrong path is caught here rather than when you submit a long run.

Requirements: QGIS 3.44 or newer (including QGIS 4.x), hydromate 0.2+, and a working
TELEMAC and/or OpenFOAM installation. Nothing is bundled - no solver, no MPI, no
scientific Python stack.

.. note::

   QGIS's Python does **not** need to be the solver's Python, and should not be. The
   plugin talks to hydromate over its command line, so the two environments stay separate.

Setting up a project
--------------------

On the **Setup** tab:

1. *Add case…* and pick a ``case-config.yml``.
2. Optionally choose a **solver profile** (see :doc:`jobs`) and a **job root** - point the
   latter at a large volume.
3. *Save as…* to write a ``<name>.hydromate-prj`` beside your case.

The project file is a thin pointer: which cases belong together, which profile to use,
where jobs go. It deliberately carries **no simulation status** - the runner writes status
while QGIS is closed, so a copy here would be stale, and two open QGIS windows would fight
over it.

The tabs
--------

Between *Setup* and *Jobs*, the tabs are **generated from what hydromate reports your case
can do**, not hardcoded:

* a capability that does not apply to your solver is **hidden** - OpenFOAM's free surface
  is inherently 3D and transient, so a "Steady 2D" tab under it would be a category error;
* one hydromate has not implemented yet is **shown disabled, with the reason**;
* the buttons enable from whether the case is configured, built and run.

Adding a capability to hydromate makes it appear here with no plugin update.

Each tab offers *Build*, *Submit* and *Load results*, plus the few options worth changing
per run (process count, and so on). Everything else stays in ``case-config.yml``, where it
is documented.

The job dashboard
-----------------

.. code-block:: text

    Job ID                              Case   Solver   Kind    State      Progress
    2026-08-14-isar-2025-steady-a3f19c  isar   TELEMAC  steady  RUNNING    62%  (it 4100)
    2026-08-13-isar-2025-bal-9ab19d     isar   TELEMAC  bal     COMPLETED  iter 50/50  best 0.0198
    2026-08-12-isar-2025-meshconv-5b31  isar   TELEMAC  meshc.  FAILED     level 3/4

Actions: **Refresh**, **Cancel**, **View logs**, **Open job directory**, **Load results**.

Cancelling stops the whole process tree, MPI ranks included, and gives the job the chance
to shut down cleanly first. A failed job's error - with its suggested remedy - is on the
row's tooltip and in the status line.

The table refreshes itself while something is running and goes quiet when nothing is: it
polls only the visible non-terminal rows, checks a file's timestamp before reading it, and
stops the timer entirely once every job has finished.

Closing QGIS, and coming back
-----------------------------

This is the workflow the whole architecture is for:

1. Submit some jobs.
2. Close QGIS. **The solvers keep running.**
3. Come back hours or days later, open QGIS and load your ``.hydromate-prj``.
4. The jobs are rediscovered with the state they have actually reached - including any
   that failed or were killed by a reboot while you were away.
5. Load the results.

Results
-------

*Load results* adds a job's output under ``hydromate/<job id>`` in the layer tree,
referencing the files where the solver left them - nothing is copied, which matters when a
reconstructed OpenFOAM case is several gigabytes.

Two defaults are applied:

**Water depth**
    White → bright blue → dark blue, with everything below your minimum depth
    **transparent**. That threshold is not decoration: on a bed with a Nikuradse roughness
    of 0.05-0.5 m, water 5 mm deep stands *inside* the grain roughness rather than flowing
    over it, so drawing it as river overstates the wetted extent. hydromate's own reports
    use the same filter, so the map and the report agree.

**Velocity**
    Arrows over a reversed *plasma* ramp, capped at 5 m/s and **warned about** above it.
    A depth-averaged river result above that is nearly always a wetting/drying artefact in
    a nearly-dry cell, and letting one such node set the scale flattens the whole map.

Both thresholds are in *Settings*. Everything else points you at Layer Symbology, on
purpose: a plausible-looking wrong scale is harder to notice than an obviously default one.

For an unsteady result, **Export movie** renders the visible variable frame by frame and
encodes WebM/VP9 with ``ffmpeg``. Without ffmpeg the PNG frames are kept and the exact
command to encode them is shown. To export rasters, use QGIS's own mesh export.

The print layout
----------------

*HydroMate ▸ Add the default A3 print layout* creates a layout fitted to your current
extent: the ROI, a north arrow, a bold **Q** arrow along the reach, a two-tone scale bar,
an empty legend with a hint to populate it, and 16 pt Arial throughout.

Processing
----------

Three algorithms, in the toolbox and the Graphical Modeler:

``Submit a simulation job``
    Validates, creates the job, submits, returns the id - in about a second. It **does
    not** run the simulation.
``Check job status``
    Reports state and progress. Returns immediately; it does not wait.
``Import job results``
    The same styled loading as the dashboard button.

Together they make a discharge sweep scriptable without writing plugin code.

Troubleshooting
---------------

**"hydromate could not be found"**
    The message names all three places it looked: the plugin setting, ``$HYDROMATE_EXE``
    and ``PATH``. Set the path in *Settings* and press *Test*. There is deliberately no
    silent fallback to some other interpreter.

**A job fails immediately**
    *View logs*. ``runner.log`` carries the error and a suggested remedy; the solver's own
    listing is behind the toggle. ``hydromate profiles validate`` checks the environment.

**A job seems stuck**
    It will not stay stuck. hydromate reconciles a recorded process that has gone - after a
    crash, a reboot or ``wsl --shutdown`` - and reports FAILED rather than leaving a job
    that can never finish. Press *Refresh*.

**A layer loaded but is not styled**
    The dataset name did not match. Style it by hand from Layer Properties; the file itself
    is fine.

**No movie**
    Install ``ffmpeg``. The frames are kept regardless.

Reporting a problem
-------------------

Please include your QGIS version, ``hydromate --version``, and the tail of the job's
``runner.log``: https://github.com/sschwindt/hydromate/issues
