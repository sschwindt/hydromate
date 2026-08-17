Jobs: running simulations that outlive their shell
==================================================

A hydraulic simulation runs for hours or days. Tying it to the lifetime of a terminal, an
SSH session or a QGIS window makes it fragile in exactly the way that costs the most: a
run that is nearly finished. The job system exists so that a submitted simulation keeps
going, can be found again, and can be stopped cleanly.

Quick start
-----------

.. code-block:: bash

    # submit; prints the job id and returns immediately
    axqua submit cases/example-Inn/case-config.yml --kind steady

    axqua list                        # what exists
    axqua status <JOB_ID>             # where it is
    axqua logs   <JOB_ID> --follow    # what it is saying
    axqua cancel <JOB_ID>             # stop it and everything it started

Close the terminal, log out, reboot QGIS - the job carries on. Add ``--json`` to any of
these for machine-readable output.

Job kinds
---------

One per unit of long-running work, matching the per-case scripts:

=========================  ==========================================================
kind                       what it does
=========================  ==========================================================
``preprocessing``          build the TELEMAC case (mesh, boundaries, steering files)
``steady``                 run the steady 2D case
``mesh-convergence``       the grid-independence study
``build-3d``               write the TELEMAC-3D steering files
``steady-3d``              run a 3D case
``vertical-convergence``   the sigma-layer study
``unsteady``               the hydrograph-driven run
``openfoam-build``         mesh and write the interFoam case
``openfoam-run``           checkMesh, decompose, the two stages, reconstruct
``calibration``            HydroBayesCal, single or multi-flow
=========================  ==========================================================

``axqua submit --help-kinds`` lists them at any time.

Per-kind options are the knobs that used to be constants at the top of each case script::

    axqua submit case-config.yml --kind mesh-convergence \
        --option tolerance=0.05 --option auto_extend=true --option ncsize=16

An unknown option is refused before anything is created, because a job that *looks*
submitted and quietly runs something else is worse than one that will not start.

What a job looks like on disk
-----------------------------

.. code-block:: text

    <job_root>/2026-08-14-isar-2025-steady-a3f19c/
      job.json          what was asked for, frozen (read-only after writing)
      status.json       where it is now (runner-only, replaced atomically)
      runner.log        axqua's narration, rotated
      solver/           the solver's own listing, rotated separately
      input/            the frozen case-config.yml, and a build's working folders
      results/
        results.json    a small manifest: paths plus how to style them
        qgis/           GIS-ready exports
      hydrobayescal/    calibration artifacts

The identifier is readable and sortable on purpose: it is a directory name and a dashboard
column, and ``f835ac7d-3e91-...`` is neither greppable nor meaningful six months later.

**The directory is authoritative.** A SQLite index at
``~/.local/share/axqua/jobs.sqlite`` makes listing hundreds of jobs fast, but it is a
cache: ``axqua list --rebuild`` reconstructs it by scanning, and every index failure is
swallowed so that bookkeeping can never take a job down.

``job.json`` freezes the **resolved configuration**, not a pointer to it. Editing
``case-config.yml`` afterwards cannot change a running job, and a finished job stays
reproducible from its own folder.

States
------

.. code-block:: text

    QUEUED -> STARTING -> RUNNING -> POSTPROCESSING -> COMPLETED
                                 \-> FAILED
    (any active state) -> CANCEL_REQUESTED -> CANCELLED

Two edges look wrong until you think about them, and both are deliberate:
``QUEUED -> CANCELLED`` directly (nothing was ever launched, so there is nothing to tear
down) and ``CANCEL_REQUESTED -> COMPLETED`` (a cancel is a *request*, and a solver seconds
from finishing may finish first; reporting that as cancelled would be a lie).

Progress is **typed by kind** rather than flattened: a solver run reports simulated time
against duration, a convergence study reports levels, a calibration reports iterations and
a best objective. That is why the dashboard's progress column reads sensibly whatever the
job is.

**A job never sticks in RUNNING.** If the recorded process is gone - a crash, a reboot,
``wsl --shutdown``, an OOM kill - the next status or list reconciles it to FAILED with an
explanation. PID reuse is detected too, by comparing the process start time as well as the
number.

How detachment works
--------------------

The environment is captured from the solver's setup script **once**, at submit, and set
directly on the detached process. No shell sits inside the job's process tree, which is
what makes cancelling it reliable.

=============  ================================================================
launcher       how
=============  ================================================================
``systemd``    a transient user unit; stopping it kills the whole cgroup, MPI
               ranks included. Preferred on Linux.
``posix``      a new session, so the process group covers every descendant.
               The fallback where no user manager is available.
``windows``    a **named** Job Object, so a later ``cancel`` from a different
               process can re-open and terminate it.
``wsl``        detached *inside* the distro; monitored and cancelled through
               ``wsl.exe``. Killing ``wsl.exe`` does **not** kill the Linux
               tree - it is a client, not the parent.
=============  ================================================================

Chosen automatically; override with ``--launcher``.

.. note::

   The Windows and WSL launchers are implemented and unit-tested, but have not been
   executed against a real Windows or WSL installation. Reports welcome.

Cancellation is cooperative first: ``cancel`` writes a request file, the running job
notices it, tears itself down and records ``CANCELLED`` - which lets it close its files
and note where it got to. Only if it does not respond within the grace period does the
launcher kill the tree.

Solver profiles
---------------

Machine-local, in ``~/.config/axqua/profiles.yml`` - never in a project file, because
these are install paths and would destroy its portability:

.. code-block:: yaml

    profiles:
      telemac_linux:
        solver: telemac
        environment: posix
        setup_script: /opt/telemac/v9.1/configs/pysource.sh
        mpi_processes: 16
        working_root: /scratch/axqua

      openfoam_windows:            # Foundation OpenFOAM: WSL is the route
        solver: openfoam
        environment: wsl
        distro: Ubuntu-22.04
        setup_script: /opt/openfoam9/etc/bashrc   # a path INSIDE the distro
        working_root: /scratch/axqua          # inside the distro

.. code-block:: bash

    axqua profiles list
    axqua profiles validate            # can these actually reach their solvers?

``validate`` is the check to run **when you write the profile**, not when you submit a
six-hour job. It also warns when a solver variable came from your ambient shell rather than
from the setup script - a detached job does not inherit an interactive environment, so that
is precisely the "works in my terminal, fails in the runner" trap.

Profiles are optional. A case that already carries ``telemac.pysource`` needs none.

Where things are kept
---------------------

``~/.config/axqua/``
    ``profiles.yml`` - hand-edited, worth backing up.

``~/.local/share/axqua/``
    ``jobs.sqlite`` and the default job root - derived, safe to delete.

The job root, first hit wins
    ``--job-root`` → ``$AXQUA_JOB_ROOT`` → the profile's ``working_root`` → the case
    config → ``~/.local/share/axqua/jobs``.

Platform-aware: ``%APPDATA%`` / ``%LOCALAPPDATA%`` on Windows,
``~/Library/Application Support`` on macOS. Point the job root at a large volume - CFD
output is measured in tens of gigabytes.

Debugging
---------

Run a job **synchronously** in the current shell, which is exactly what a detached job
does internally, so a failure reproduces under a terminal:

.. code-block:: bash

    axqua execute <JOB_ID>          # or a job directory, or its job.json

Then read, in this order:

``runner.log``
    aXqua's narration: state transitions, the commands it issued, exit codes. Where a
    *setup* problem is explained.
``solver/*.log``
    The solver talking. Where a *numerical* problem is diagnosed. ``aXqua logs
    <JOB_ID> --solver``.
``status.json``
    The structured error, with a stable ``code``, the ``subject`` at fault and a
    ``remedy``.

Exit codes are per category, so a script can branch without parsing text: 2 config,
3 geodata, 4 environment, 5 solver, 6 mesh, 1 anything unanticipated, 130 cancelled.
