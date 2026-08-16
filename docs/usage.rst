Usage
=====

``hydromate`` is driven by a single YAML configuration file pointing at your
:doc:`input files <input_files>`. **One case description, two simulation backends**:
the geodata, the boundary conditions, the roughness, the structures and the ground
truth are shared, and each solver adds only the knobs that are genuinely its own.

What a case can do
------------------

Every case carries one marker file per solver at its top level, refreshed by any
build and by ``hydromate status``:

.. code-block:: bash

   hydromate status cases/example-Inn/case-config.yml          # summary + refresh
   hydromate status cases/example-Inn/case-config.yml --full   # the whole table
   hydromate status cases/example-Inn/case-config.yml --check-env   # probe the solvers

.. code-block:: text

   cases/example-Inn/
     MODEL=TELEMAC_ENABLED     # the NAME says whether the CASE declares this solver,
     MODEL=OPENFOAM_DISABLED   # so it means the same on any machine

Each capability is reported on three axes, which answer three different questions and
have three different fixes:

.. list-table::
   :header-rows: 1
   :widths: 18 42 40

   * - axis
     - question
     - values
   * - ``implemented``
     - does hydromate support this **for this solver**?
     - ``yes`` / ``no`` (not yet) / ``n/a`` (never - OpenFOAM has no depth-averaged mode)
   * - ``configured``
     - does **this case** ask for it?
     - from the config alone (a varying inflow implies ``unsteady2d``)
   * - ``built`` / ``run``
     - do the **artifacts** exist?
     - ``steady2d.cas`` written, ``r2d.slf`` produced

Cells that cannot arise render ``-``, never a confident ``no``. The files are
generated, so they are gitignored - they describe the *currently available* setup,
which is local state like ``hydromate-case/``.

The general workflow
--------------------

Steps 1-3 are **shared**: the same inputs, the same commands, whichever solver you
end up running.

#. **Prepare the input files** - the :ref:`geodata <input-geodata>`, the
   :ref:`ground truth <input-ground-truth>` (generate the
   :ref:`calibration-target template <input-target-template>` with ``hydromate
   targets <config>`` and fill it in), and the :ref:`config YAML <input-config>`.
#. **Describe the reach once** - the ROI polygon, the liquid boundaries, the mesh
   zones and centerline, the roughness zones, the discharge and the outflow
   condition, and any :ref:`structures <usage-structures>` (dams, weirs, walls,
   buildings). Both meshers read exactly these.
#. **Check what is set up** - ``hydromate status <config>``.

From there the two backends diverge, because they answer different questions:

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * -
     - **TELEMAC**
     - **OpenFOAM (interFoam)**
   * - answers
     - depth-averaged flow, morphodynamics, calibration - the whole reach, many runs
     - the vertical structure of the flow where the surface has a real gradient
   * - mesh
     - anisotropic triangles, flow-aligned (gmsh/BAMG) -> ``geometry.slf``
     - terrain-following all-hexahedral lattice -> ``constant/polyMesh``
   * - free surface
     - a state variable of the shallow-water equations
     - a resolved two-phase (VOF) interface
   * - cost
     - hours
     - hours to days; see the cost report the build prints
   * - run it when
     - always - it is also the 3D run's hotstart
     - after the 2D run has converged

The TELEMAC workflow
--------------------

.. code-block:: bash

   python cases/example-Inn/preprocessing.py            # build the case
   python cases/example-Inn/initial_run.py              # test-run + hotstart convergence
   python cases/example-Inn/mesh_convergence_study.py   # grid-independence study
   python cases/example-Inn/run_Bayes_cal.py            # Bayesian calibration

#. **Preprocessing / build** (``preprocessing.py``, or ``hydromate <config>``) -
   clip the DEM(s), build the mesh + bathymetry, classify the liquid boundaries,
   and write the complete TELEMAC case (``geometry.slf``, ``boundaries.cli``,
   ``friction.tbl``, ``steady2d.cas``) plus the calibration CSV and HydroBayesCal
   ``config_Telemac.py``. No solver is launched. This is also where everything that
   depends on the *geometry* is decided and logged, so it can be checked before any
   compute is spent: the :ref:`initial condition <usage-initial-condition>` (seeded
   at the normal-flow stage of real cross-sections), the outflow rating, and - for a
   :ref:`gain-lose reach <usage-gain-lose>` - the water table, the exchange faces it
   implies and the discharge they would carry.
#. **Initial run** (``initial_run.py``) - test-run the built case once to confirm
   it does not crash, and check that the boundary fluxes have reached mass balance
   (the hotstart convergence check). The solver's output streams live with a
   progress bar (see `The initial run`_). This concludes preprocessing.
#. **Mesh-convergence study** (``mesh_convergence_study.py``) - the
   grid-independence study, worth starting only **once the initial run has confirmed
   the model runs**.
#. **Optional extensions** - ``add3d.py`` (TELEMAC-3D sigma layers) then
   ``vertical_convergence_3d.py``; ``unsteady_run.py`` for a hydrograph.
#. **Calibration & validation** - hand the built case to HydroBayesCal (:doc:`hbc`).

The OpenFOAM workflow
---------------------

Use it when the **vertical structure** of the flow matters *and* the water surface
has a gradient - which rules out ``simpleFoam`` and means a two-phase VOF solver.

**TELEMAC runs first, automatically.** OpenFOAM is the expensive model in the chain,
and almost everything it needs to know at ``t = 0`` is something a 2D depth-averaged
run answers in minutes: where the water is, how deep it is, how fast it moves, and
roughly where its surface sits. So the build seeds itself - it reuses the case's own
converged ``r2d.slf`` when there is one, and otherwise builds and runs a **coarse
TELEMAC pre-run** of its own (54 s on isar-2025 at ``size_scale: 4``, producing a
3D mesh within 3% of the one the full production result gives). That seed does four
jobs at once: the wetted cells start wet, the lid is clamped just above the surface
so most air cells never exist, the plan footprint is trimmed to the wetted corridor,
and every column starts with a velocity.

Switch it off with ``openfoam.pre_run.enabled: false``, ``--no-pre-run``, or the
``PRE_RUN = False`` toggle at the top of ``openfoam_preprocessing.py``. The case is
then built **cold** under a flat lid, which is what happens without a seed: many
times more air cells, plus the whole filling transient, in the slowest solver you
have.

**The seed is an approximation, and is treated as one.** TELEMAC is a model too. The
3D free surface is still solved and free to move away from the 2D answer - that is
what ``freeboard`` (how far it may rise) and ``wet_margin`` (how far it may spread)
are room for. Under ``headroom_mode: auto`` both are sized from the seed's own flow
rather than guessed: the velocity head ``V^2/2g`` the water could convert at a
stagnation point or the outside of a bend, plus a quarter of the depth for what a
depth-averaged model cannot see at all, with the configured values as **floors**. And
after the run, ``surface_freedom`` reports whether the water ever reached the lid or
the lateral wall - i.e. whether the answer was set by a meshing decision rather than
by the hydraulics, which nothing else in the output would reveal.

.. code-block:: bash

   hydromate openfoam cases/example-Inn/case-config.yml --check   # cell count, no build
   python cases/example-Inn/openfoam_preprocessing.py             # build the case
   python cases/example-Inn/openfoam_run.py                       # spin-up, run, report

#. **Build** - a terrain-following, all-hexahedral mesh written straight to
   ``constant/polyMesh`` (no snappyHexMesh: a river bed is a height field, so it is
   *followed* rather than snapped to), fields seeded from the converged ``r2d.slf``,
   and the two staged dictionary sets. The build prints its own **cost report** -
   the time step the Courant target will settle on, the number of steps your
   ``end_time`` implies, and how that compares with one flush of the reach.
#. **Run** - ``checkMesh``, ``decomposePar``, then two stages of ``interFoam``:
   a short spin-up that settles the interface from the depth-averaged hotstart,
   then the production stage. Watch ``Co`` and ``dt`` on the progress bar; a healthy
   run holds ``dt`` near the Courant target.
#. **Report** - inlet/outlet **water** discharge and their relative imbalance,
   judged against the same ``hydrodynamics.flux_tolerance`` the 2D run is judged by,
   plus the **surface-freedom** verdict above.

For the vertical structure of the seed itself, ``pre_run.dimension: 3d`` follows the
2D pre-run with a hydrostatic TELEMAC-3D run and seeds each OpenFOAM cell at its own
elevation, instead of giving every column one depth-averaged velocity from bed to
surface. That is the one case where prescribing vertical structure beats starting
flat: it was computed by a solver on this reach's own bathymetry, not assumed from a
log law whose normalisation is inadmissible on a gravel bed anyway.

**It is not available on every reach, and hydromate checks before it spends the run.**
TELEMAC-3D has only sigma planes, and their count is sized from the flow depth against
the horizontal cell size, refusing cells more than four times taller than wide. A
reach that is shallow relative to its plan mesh therefore has no room for an interior
level at all: isar-2025 is 0.26 m deep with 0.62 m cells and gets exactly **two**
planes - one layer, which is a depth-averaged answer at 3D cost. Asking for
``dimension: 3d`` there returns in seconds with that reason and keeps the 2D seed. To
make it available, the reach has to be deeper, or the plan mesh coarser.

Where it does run, the seed run is deliberately made **robust rather than faithful**:
it cold-starts at a constant depth rather than continuing the 2D surface (which, on a
braided bed, lifts a great many dry columns onto the sigma mesh and diverges on the
first solve), and it uses k-epsilon rather than Spalart-Allmaras (which diverges in
the vertical diffusion of velocity on a wetting/drying bed). A seed needs a plausible
profile, not agreement with the 2D surface. The result is checked for NaN before it is
adopted.

The air phase is the usual reason such runs fail, so three things address it: the
**lid follows the 2D free surface** at a fixed ``freeboard`` so most air cells never
exist; **semi-implicit MULES** lets the Courant target run near 0.9 instead of the
tutorials' 0.2; and a **``limitVelocity`` constraint** caps ``|U|`` at several times
the reach's own water speed - water never reaches it, a runaway air jet does.

.. _usage-rigid-lid:

Getting there cheaply: the rigid lid
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Even tamed, the two-phase run is expensive, and most of what goes wrong first - a
mesh ``checkMesh`` rejects, an inlet that is not where you thought, a roughness that
is too coarse for the depth - has nothing to do with the air. ``openfoam.mode:
rigid-lid`` removes the air phase **by construction** rather than damping it: the lid
is placed *on* the 2D free surface as a ``slip`` wall, so the domain holds water only
and ``alpha`` is identically 1. ``interFoam`` then degenerates to a single-phase run
while ``p_rgh`` and gravity stay exactly as they are, which is what keeps the result a
recognisable river rather than a duct flow.

Three consequences follow, and together they are the speed-up:

* there is **no interface**, so no interface Courant limit and no spin-up stage;
* ``alpha`` has nothing to advect, so the implicit MULES correction is dropped -
  measured at four fifths of the run time on its own;
* the cells that were air - about nine tenths of them - are simply absent.

Combine it with ``cell_size_factor``, which coarsens relative to the 2D channel
resolution so a test run cannot silently drift away from the mesh being tested:

.. list-table::
   :header-rows: 1
   :widths: 20 12 20 16 32

   * - mode
     - plan dx
     - cells
     - time step
     - 300 s of river (isar-2025, 1 core)
   * - ``vof``
     - 0.5 m
     - 1,579,074
     - 3.1e-3 s
     - ~97,600 steps
   * - ``rigid-lid``, factor 3
     - 1.5 m
     - 36,164
     - 1.8e-1 s
     - ~1,700 steps, ~8 min (60 s measured in 96 s)
   * - ``rigid-lid``, factor 5
     - 2.5 m
     - 14,632
     - 3.7e-1 s
     - 832 steps, **91 s wall-clock**

**What you give up is the free surface.** It can no longer move, so the mode cannot
tell you about a hydraulic jump, a standing wave, or superelevation through a bend -
exactly the questions a 3D free-surface model is usually bought for. It is a way to
reach a trustworthy ``vof`` run quickly, not a substitute for one.

Two knobs exist only in this mode. ``min_water_depth`` (0.20 m) leaves shallower
columns out of the mesh; it cannot be raised freely, because a reach whose inflow
section is itself shallow will lose its inlet patch to the trim. The layer count is
then **fitted to that depth** - cells thinner than the bed grains are meaningless, and
thin enough that the bed's variation within one plan cell folds them - so 14 layers
over 0.20 m is reduced to 4, and the build says so. ``auto_bed_layer`` defaults *off*
here for the same reason: pinning a thick bed layer into a shallow column is what
turns the layers above it into folded slivers.

Prefer a form to hand-editing the YAML? Launch the browser-based configuration
editor with ``hydromate-gui`` (see :ref:`the graphical configurator <input-config>`);
its **Build** button is the same build step as above.

.. _usage-structures:

Structures: dams, weirs, walls and buildings
--------------------------------------------

A structure is an **ordinary QGIS vector layer**, not a triangulated surface. STL is
a ``snappyHexMesh`` requirement; hydromate writes its mesh itself, so a structure only
has to say **where its footprint is** and **how high it stands** - both ordinary
attributes. There is no CAD step and no format QGIS cannot author or round-trip.

Draw it either way:

* a **polygon** is the footprint directly (a building, a dam body, a pier);
* a **line** is buffered by its ``Width (m)`` attribute - the natural way to draw a
  wall or a dam crest, tracing the crest and saying how thick it is rather than
  digitising two parallel sides.

Say how high it stands in one of two ways, and the field you fill in is what decides:

* a **crest elevation** (m a.s.l.) gives a *level* crest - a dam, a weir, a floodwall;
* a **height** (m above the local ground) gives a crest that *follows the terrain* -
  an embankment or levee of constant build height.

Two modes, and the choice is hydraulic:

.. list-table::
   :header-rows: 1
   :widths: 16 42 42

   * - mode
     - what it is
     - what the mesh does
   * - ``overflow``
     - dam, weir, embankment, levee, block ramp - water passes over it
     - the **bed is raised to the crest**; the plan mesh is unchanged. Identical in
       both solvers.
   * - ``solid``
     - wall, floodwall, building, pier - never overtopped
     - OpenFOAM **removes the footprint**, so its sides are no-slip walls from bed to
       lid. TELEMAC has no vertical wall to remove, so the bed is raised to crest +
       ``structures.solid_freeboard_2d`` instead - the standard 2D practice.

The mode is taken from the ``Type`` text by substring (``dam``/``weir``/``levee``…
vs ``wall``/``building``/``pier``…), or set explicitly per feature in a ``Mode``
column. Anything unrecognised is ``overflow``: raising the bed is the conservative
failure, since it keeps the domain connected and lets water pass, whereas wrongly
blanking a footprint would silently wall off part of the reach. A solid structure
that cuts the domain in two is reported as a warning, not applied silently.

.. code-block:: yaml

   geodata:
     structures: user-sources/geodata/structures.gpkg

   structures:
     type_field: "Type"           # dam | weir | wall | building | embankment ...
     mode_field: "Mode"           # optional per-feature override: solid | overflow
     crest_field: "Crest (m)"     # LEVEL crest elevation [m a.s.l.]
     height_field: "Height (m)"   # or: height above the local ground [m]
     width_field: "Width (m)"     # line features only
     default_width: 1.0
     solid_freeboard_2d: 2.0      # TELEMAC: how far above the crest a solid is raised

The initial run
---------------

``initial_run.py`` launches the built ``steady2d.cas`` **once** (no rebuild) to
confirm the case runs, then checks the boundary-flux mass balance (the hotstart
convergence check; see :doc:`hbc`):

* **Live output + progress bar.** The solver's listing is streamed to the terminal
  as the run marches - rather than captured silently - and a single-line progress
  bar tracks the **simulated time against the run's** ``DURATION``, the point the
  variable-time-step march reaches when no stop criterion fires. Because the
  CFL-adaptive step makes the *number* of time steps unknown in advance, progress is
  measured against that simulated-time cap, with the live iteration count shown
  alongside (:class:`hydromate.progress.SolverProgress`, wired by
  :func:`hydromate.run_solver_streaming`). **Every** solver launch in the workflow
  streams the same way: the per-mesh runs of the mesh-convergence study, the
  per-layer runs of the vertical convergence study, and the ``--run`` modes of
  ``add3d.py`` / ``unsteady_run.py``.
* **Core-count override.** Set the module-level ``NCSIZE`` at the top of
  ``initial_run.py`` to run this test on a different number of MPI processes than the
  ``telemac.n_processors`` chosen during preprocessing; leave it ``None`` to use the
  configured count. The command run is exactly
  ``telemac2d.py steady2d.cas --ncsize=<N> -s --nozip``, so the wrapper adds no
  compute overhead versus launching TELEMAC by hand.
* **Flux convergence + the generated hotstart case.** After the run,
  :func:`hydromate.analyze_flux_convergence` reads the
  ``.sortie`` listing and writes ``extracted-fluxes.csv`` / ``flux-convergence.png``
  (the per-boundary fluxes) and ``convergence-rate.csv`` / ``convergence-rate.png``
  (the relative imbalance and its rate) into ``simulation/``; the per-processor
  ``*_p0000N.sortie`` copies of a parallel run are deleted (only the merged main
  listing matters). When the **absolute** flux imbalance ``||Q_in| - |Q_out||``
  stays below 1e-3 m³/s over 10 consecutive listing printouts (or, on a noisy
  steady state, in the 10-printout mean), a ``hotstart2d.cas`` is generated next to
  the steady case: it continues from ``r2d.slf`` with that steady time as
  ``DURATION`` and the constant Q/H prescriptions unchanged
  (:func:`hydromate.steering.write_hotstart_cas`).
* **Where the water is** (:func:`hydromate.report_wetting`). A balanced flux budget
  says nothing about wetted *extent*, and the two are independent failure modes: a
  run can close its budget to 1e-4 and still show water standing where the reach has
  none. ``wetting-report.csv`` splits the wetted area into **active** flow, stagnant
  **film** and **isolated puddles**, says how much of each the initial condition put
  there, and - by re-reading earlier frames - whether the film is still draining or
  has **plateaued**. The distinction matters because a 2D model has neither
  infiltration nor evaporation: water seeded above the level the run converges to
  can never leave, so a plateaued film is a defect no amount of extra runtime will
  fix. Water an external source holds in place (a
  :ref:`water-table pool <usage-gain-lose>`) is reported separately, so it does not
  read as a defect. ``outlet-profile.csv`` then bands the flowing nodes by distance
  from the outflow and compares the near-boundary surface slope with the reach's
  own, returning ``backwater`` (the prescribed stage is holding water up over ground
  that should be dry), ``drawdown`` or ``neutral``.

  The report's wetted threshold is ``hydrodynamics.wet_depth`` (0.01 m). This is a
  *reporting* convention rather than a model setting: on a bed with Nikuradse
  ``ks`` 0.05-0.5 m, water 5 mm deep stands *within* the grain roughness rather than
  flowing over it. Filter the result in ParaView at the same depth so the picture and
  the report agree. To remove such water from the model instead, see the ``drying``
  block.
* **Discharge across your own cross-sections** (:func:`hydromate.report_sections`).
  With ``geodata.control_sections`` set, each line of that layer is integrated from
  the result (``Q = int (H*U).n ds``) into ``baffle-XS-q.csv``. Because it reads the
  *result* rather than the steering file, sections can be drawn and moved in GIS
  without re-running the solver - which is how the split of the total discharge
  between the threads of a braided reach is read off and checked against field
  transects.

Optional 3D extension (after the 2D path)
-----------------------------------------

A 3D simulation builds on the 2D one and is run **only after it**: it needs the 2D
**hotstart** result (``r2d.slf`` from the initial run) and reuses the horizontal mesh
whose resolution the mesh-convergence study has already validated.

#. **3D cases** (``add3d.py``) - write exactly **three** TELEMAC-3D steering files
   hotstarted from the 2D result (:func:`hydromate.build_3d_cases`); the number of
   sigma layers and the turbulence model are inferred from ``r2d.slf`` and the fixed
   time step is sized for a Courant number of 0.6:

   * ``hotstart3d_hydrostatic.cas`` - ``NON-HYDROSTATIC VERSION : NO`` (the cheap
     hydrostatic solver), constant in-file Q/H, ~30k fixed steps with a short
     listing period: the steady **boundary-flux convergence check** (the sortie's
     ``FLUX BOUNDARY`` printouts show when in/outflow balance in 3D).
   * ``hotstart3d_hydrodyn.cas`` - ``NON-HYDROSTATIC VERSION : YES``; the steady
     non-hydrostatic run with the same in-file prescribed Q (inflow) and H (outflow).
   * ``unsteady3d.cas`` - non-hydrostatic and hydrograph-driven: the time-variable
     inflow Q(t) and the stage-discharge outflow SL(t) come from the **same**
     ``LIQUID BOUNDARIES FILE`` as ``unsteady2d.cas`` (needs a *varying*
     ``boundaries.inflow`` series; skipped with a notice otherwise).

   ``--run [hydrostatic|hydrodyn|unsteady]`` also launches ``telemac3d.py`` on one
   of them (default: hydrostatic, the flux-convergence check).
#. **Vertical-layer convergence** (``vertical_convergence_3d.py``) - the number of
   vertical layers (``dz``) is a *new* discretization choice the 2D mesh-convergence
   study never covered, so it gets its **own** grid-independence study over the layer
   count, the 3D analogue of the mesh-convergence step.

.. code-block:: bash

   python cases/example-Inn/add3d.py                    # write (and --run) the 3D cases
   python cases/example-Inn/vertical_convergence_3d.py  # vertical-layer convergence

The ``cases/example-Inn/`` scripts drive the worked Inn example from its
``case-config.yml``; copy the data-free ``cases/case-template/`` folder to start
your own case.

.. _usage-gain-lose:

Gain-lose reaches (flow through a porous body)
----------------------------------------------

Some reaches lose flow into a porous body - a gravel bar, an alluvial patch - and
regain it downstream. A 2D depth-averaged model has no subsurface, so ``hydromate``
represents the underflow as an internal **withdrawal** where water infiltrates plus
an **injection** where it resurfaces, generated into a TELEMAC ``USER_RAIN`` routine
(``FORTRAN FILE``; TELEMAC compiles it at run time). Enable it by pointing
``gain_lose.zone`` at the porous body:

.. code-block:: yaml

   gain_lose:
     enabled: true
     zone: user-sources/geodata/porous-body.gpkg
     conductivity: 3.0e-4        # kf [m/s]
     water_table: phreatic

The generated routine is **depth-limited** - the withdrawal tapers to zero as a cell
approaches ``min_depth`` and is additionally capped at half the water available that
step - so a sink can never dry a cell. TELEMAC's own source terms carry no such
guard, and an unguarded sink drying marginal cells spikes the velocities and pins the
CFL-adaptive time step at a value from which the run never recovers. It is also
**mass-exact**: whatever is withdrawn is reinjected in the same step, so no net sink
appears in the boundary budget - which matters beyond tidiness, because a permanent
sink ``S`` makes the steady budget read ``|Q_in| - |Q_out| = S`` and puts a floor
under the relative flux imbalance, so no hotstart case would ever be written again.

**Where the exchange happens** is ``gain_lose.faces``:

``water-table`` (the default)
   The faces follow the physics and **nothing has to be drawn**. The body's saturated
   zone is bounded by the two channel levels it exchanges with, so its water table is
   known (:mod:`hydromate.watertable` fits it as a plane, taking those levels from
   the channel surface at each end of the zone's reach extent). A node then **loses**
   where it is wet and its free surface stands above the table, and **gains** where
   the table stands above the bed. The classification runs *inside* the generated
   routine, so the faces move with the stage - a rising river widens the losing face,
   which a build-time mask cannot represent. Needs ``geodata.channel_centerline``.
   Prefer this: it is genuinely fuzzy where percolation begins and ends, so asking
   for the faces to be drawn asks for a number nobody has.

``lines``
   The exchange is pinned to the ``int-*`` lines of
   ``boundaries.liquid_boundaries``, each buffered to a strip. Pick this when the
   location is actually known - a surveyed seepage face, a spring line - or to
   reproduce a calibrated exchange.

**How big it is** is either ``conductivity`` or ``discharge``, and both work with
either geometry. ``conductivity`` (``kf``) drives it by default through Green-Ampt's
saturated limit ``f = kf (h + Lz + hf) / Lz`` at the local head, so the exchange
*responds to water level*; ``discharge`` overrides that with a measured total,
normalised over the losing face.

.. note::

   Riverbed ``kf`` is not a textbook constant - it spans 1e-9 to 1e-2 m/s with grain
   size and colmation, and varies within a single site (Calver, A., 2001, *Riverbed
   Permeabilities: Information from Pooled Data*, Ground Water 39(4), 546-553,
   `doi:10.1111/j.1745-6584.2001.tb02343.x
   <https://ngwa.onlinelibrary.wiley.com/doi/10.1111/j.1745-6584.2001.tb02343.x>`_).
   As a starting bracket: clean open-framework gravel 1e-2..1e-1, moderately colmated
   gravel 1e-4..1e-3, strongly colmated or silted 1e-7..1e-5. Because the plausible
   range is this wide, ``kf`` is exposed to HydroBayesCal as the calibration
   parameter ``POROUS ZONE kf (gain-lose)`` - pick a bracket from the reference and
   let the calibration find the value rather than assuming one.

The water table does one more thing worth knowing. With ``water_table: phreatic`` the
pre-wet seeds any ground lying below it, so **closed depressions on the bar start
full**. They can never fill otherwise: a hollow sitting above the channel is
unreachable by surface flow, and the drainable-seed filter deliberately refuses to
seed behind a rim. The patch drain likewise tapers to zero *at the table* rather than
at an absolute depth, so it clears standing water off the bar top without emptying a
pool that cuts below the saturated zone.

.. _usage-initial-condition:

The initial condition
---------------------

The steady run starts either **dry** (the default: a thin water plug only at the
inflow line, because a fully dry bed makes TELEMAC's ``DEBIMP`` abort at a
prescribed-Q inflow) or **pre-wetted** (``initialization.prewet_depth``), which skips
marching the wetting front from the inflow and is what the mesh-convergence study
uses.

How the pre-wet surface is built matters more than it looks. With
``prewet_mode: normal-depth`` (the default) ``hydromate`` cuts a real cross-section
every ``prewet_bin_spacing`` metres along the centerline and seeds the **stage that
conveys the case discharge** through it, using the same Keulegan conveyance inversion
that builds the outflow rating. The seeded surface then tracks the surface the run
converges to.

``prewet_fill`` scales the depth above the thalweg and defaults to **0.70 -
deliberately below 1**. The asymmetry is the point: under-seeding is recoverable,
because the flow refills a pool within seconds, whereas over-seeding is not. Water
seeded above the converged surface has nowhere to go in a model with neither
infiltration nor evaporation; it drains only where it can flow, and on flat ground
over coarse gravel it stops as immobile film that survives to the end of the run.
``prewet_min_depth`` similarly leaves a node dry rather than laying down a feathered
margin, since a seed film is exactly what stalls.

Whatever the mode, the **inflow plug is re-imposed after every filter**: none of them
has any reason to keep the inflow cross-section wet, and ``DEBIMP`` aborts at t=0
without it. Check the result with the wetted-extent report from `The initial run`_.

.. _morphodynamics:

Morphodynamics (GAIA)
---------------------

Sediment transport and bed evolution are handled by **GAIA**, coupled to the
hydrodynamic run. Because a flood wave is what actually reworks the bed, GAIA is most
useful on the **unsteady** runs: enabling ``morphodynamics`` couples it to the
hydrograph-driven ``unsteady2d.cas`` **and** its 3D twin ``unsteady3d.cas`` (as well
as the steady case), sharing a single generated GAIA steering file. The coupling adds
``COUPLING WITH : 'GAIA'`` + ``GAIA STEERING FILE`` + ``COUPLING PERIOD FOR GAIA`` to
the driver ``.cas`` and writes a GAIA ``.cas`` next to it.

Turn it on in the config (or via the ``GAIA_*`` toggles at the top of
``unsteady_run.py``) and declare the sediment and bed processes:

.. code-block:: yaml

   morphodynamics:
     enabled: true
     bedload: true                 # BED LOAD FOR ALL SANDS
     suspended_load: false         # SUSPENSION FOR ALL SANDS (carried as tracers)
     bedload_formula: 1            # 1 = Meyer-Peter & Mueller
     sediment_classes:             # one entry per grain-size class
       - {diameter: 0.0008, density: 2650, shields: 0.047}
       - {diameter: 0.004,  density: 2650, shields: 0.047}
     morphological_factor: 10.0    # accelerate bed evolution over the hydrograph
     slope_effect: true            # transverse bed-slope pull on bedload
     friction_angle: 40.0          # repose angle of the sediment [deg]
     secondary_currents: true      # spiral-flow bedload deviation in bends
     active_layer_thickness: 0.1   # bed active-layer thickness [m]
     # prescribed_solid_discharges: [0.0, 0.00065]  # sediment supply per liquid boundary

The generated GAIA ``.cas`` references the **same geometry and boundary files** as
the coupled run (GAIA needs both even when coupled), enables the chosen transport
modes and emits the bed-process keywords above. Sediment-transport parameters
(Shields numbers, class diameters, …) are also the natural
:doc:`calibration <hbc>` targets - perturbed by HydroBayesCal through the
``gaia<KEYWORD>`` parameters. Run a morphodynamic case with the unsteady script:

.. code-block:: bash

   python cases/example-Inn/unsteady_run.py --run   # GAIA_ENABLED=True + MODE_3D toggles the 2D/3D run

The command line
----------------

The build step is also a one-shot CLI (the scripts wrap the same pipeline):

.. code-block:: bash

   hydromate cases/example-Inn/case-config.yml --check     # validate config + TELEMAC env only
   hydromate cases/example-Inn/case-config.yml             # build the full case
   hydromate cases/example-Inn/case-config.yml --dry-run   # build, then run the solver once to validate
   hydromate cases/example-Inn/case-config.yml -v          # verbose (per-stage) logging

Useful flags:

``--check``
    Load and validate the configuration (paths exist, required inputs present,
    TELEMAC ``pysource`` resolvable) and exit without building.
``--no-validate-env``
    Skip checking that the TELEMAC environment can be sourced (useful when only
    producing files on a machine without TELEMAC).
``--dry-run``
    After building, launch the configured solver once to confirm the case is
    accepted by TELEMAC.

The full command surface
------------------------

Building and inspecting a case:

.. code-block:: bash

   hydromate <config.yml> [--check|--dry-run|--no-validate-env|-v]  # build the case
   hydromate case-status <config.yml> [--full] [--check-env] [--json]
   hydromate openfoam <config.yml> [--check] [--cell-size <m>] [--layers <n>]
   hydromate targets <config.yml> [-o <out.xlsx>] [--force]
   hydromate clip <raster> -b <boundary> -o <out>
   hydromate rating -o <out.csv> --manning <n> --slope <S0> --width <b> --q <Q...>
   hydromate migrate <config> [-o <out.yml> | --in-place]

Running work that outlives this shell (see :doc:`jobs`):

.. code-block:: bash

   hydromate submit <config.yml> --kind <kind> [--profile P] [--np N] [--option k=v]
   hydromate execute <JOB_ID>            # synchronously, here - the debugging path
   hydromate status  <JOB_ID> [--watch]
   hydromate cancel  <JOB_ID>
   hydromate logs    <JOB_ID> [--follow] [--solver] [--path]
   hydromate list    [--state S] [--rebuild]
   hydromate profiles [list | show N | validate [N] | path]

Every command takes ``--json``, which emits one envelope
(``{"ok", "command", "hydromate", "data", "error"}``) on **stdout** with all narration on
stderr - so a caller can parse stdout unconditionally. That is what the QGIS plugin reads.

.. note::

   ``hydromate status`` is overloaded, and the argument decides. An existing **path** means
   the *case* (the historic meaning, which keeps working) and a job-id-shaped argument
   means the *job*. The two cannot be confused - a job id is never a path - and the case
   form is also spelled ``hydromate case-status`` if you would rather be explicit.

Exit codes are per error category, so a script can branch without parsing messages:
``2`` config, ``3`` geodata, ``4`` environment, ``5`` solver, ``6`` mesh, ``1`` anything
unanticipated, ``130`` cancelled.

Generating the calibration-target template
------------------------------------------

The ``targets`` subcommand writes the :ref:`calibration-target template
<input-target-template>` (``calibration-target-data.xlsx`` + a co-located
``extract_flowtracker.py`` helper) for a case, prefilled with its friction zones
and DoD status:

.. code-block:: bash

   hydromate targets cases/example-Inn/case-config.yml            # -> user-sources/ground-truth/
   hydromate targets cases/example-Inn/case-config.yml -o out.xlsx --force

Fill in the ``hydraulics`` / ``morphodynamics`` measurement tabs (the
``extract_flowtracker.py`` script populates the hydraulics tab straight from SonTek
FlowTracker2 exports) and the ``parameters`` tab, reference the file under
``ground_truth.targets``, and re-build so the calibration CSV and HydroBayesCal
config pick it up.

Clipping a raster to the region of interest
-------------------------------------------

The ``clip`` subcommand crops a single raster (e.g. a DEM) to a region-of-interest
polygon without needing a full configuration - handy for preparing inputs. The
raster is cropped in its own grid and resolution; if it has no embedded CRS, the
boundary's CRS is assumed and written onto the output.

.. code-block:: bash

   hydromate clip path/to/dem.tif -b path/to/roi.gpkg -o path/to/dem-roi-clip.tif

Options: ``--epsg <code>`` reprojects the raster to that EPSG before clipping;
``--all-touched`` keeps pixels touched by the polygon edge (the default keeps
pixels whose centre is inside the polygon). The same clipping is applied
automatically to ``geodata.dem_initial`` (and ``geodata.dem_target``) during a build.

.. _meshing:

Meshing
-------

The mesh is generated with `gmsh <https://gmsh.info>`_. Two strategies are chosen
automatically.

**Anisotropic, flow-aligned (preferred).** When you provide ``geodata.mesh_zones``
and ``geodata.channel_centerline``, the channel is meshed with triangles
**elongated along the centerline** and the floodplain with **near-equilateral**
triangles, blended smoothly between the two. This keeps cells aligned with the main
flow - reducing numerical diffusion and cross-flow artifacts - while staying
economical on the overbank.

Each mesh-zone polygon is classified by its ``Zone Name`` (case-insensitive
*substring*; see :ref:`Geodata <input-geodata>` for the field names) into one of
**three types**:

* ``channel`` - triangles elongated along the centerline (cross-channel edge = the
  zone's max edge length, stretched by ``channel_anisotropy`` along the flow);
* ``floodplain`` - near-equilateral triangles at the zone's edge length;
* ``refinement`` - near-equilateral triangles at the zone's (typically smaller)
  edge length, for **local refinement** (e.g. around a structure or a survey patch).

Where zones overlap the finest intent wins (``refinement`` > ``channel`` >
``floodplain``); anywhere outside every zone uses ``floodplain_size``.

**Per-polygon edge length.** Each polygon's target maximum edge length [m] is read
from the ``Max Edge Length (m)`` field of the mesh-zones layer, so you can size
every zone directly in QGIS without touching the YAML. The value is parsed
leniently - a decimal **point or a German comma** (``0,5``) are both accepted. The
``mesh`` block's ``channel_size`` / ``floodplain_size`` / ``refinement_size`` are
only **fallbacks**, used when that field is absent or blank for a polygon:

.. code-block:: yaml

   mesh:
     channel_size: 0.5          # fallback channel edge length (m)
     floodplain_size: 1.5       # fallback floodplain edge length (m); default outside zones
     refinement_size: 0.5       # fallback 'refinement' zone edge length (m)
     growth_ratio: 1.2          # max edge-size growth per element, channel -> floodplain
     channel_anisotropy: 4.0    # along-flow / cross-flow edge-length ratio in the channel

To add local refinement, draw a polygon in the mesh-zones layer, name it so the
name contains ``refinement``, and set its ``Max Edge Length (m)``. The edge lengths
are *targets*: the mesh is built from a metric-tensor background field with gmsh's
BAMG algorithm, which approximates the requested sizes and anisotropy. The defaults
yield a high-resolution mesh - for a ~0.3 km² reach on the order of half a million
elements - so increase the sizes while iterating.

**Isotropic fallback.** With no ``mesh_zones`` / ``channel_centerline``, the mesh
uses ``default_size`` everywhere, refined to ``breakline_size`` along
``geodata.breaklines`` and to ``region_sizes`` near MATID region points.

.. _numerics:

Numerics
--------

The steering (``.cas``) defaults are tuned for a real, steep, wetting/drying river
reach (set in the ``hydrodynamics`` config block; the initial condition below is in
the ``initialization`` block):

* **Finite elements** (``finite_volumes: false``, the default) - the classic kernel,
  with an advection scheme, a preconditioned linear solver and an explicit tidal-flats
  treatment, written **compute-stable by default** to keep the wetting/drying steady
  march from exploding. The time step is **CFL-bound** on the sub-metre channel cells,
  so ``VARIABLE TIME-STEP : YES`` + ``DESIRED COURANT NUMBER : 0.30`` let TELEMAC adapt
  the step (``time_step: 0.25`` is only the conservative start step). The robustness
  knobs are ``IMPLICITATION FOR DEPTH/VELOCITY : 0.80`` (config ``implicitation``),
  ``DISCRETIZATIONS IN SPACE : 11;11`` (linear FE, required by the distributive
  advection scheme 14), ``NUMBER OF SUB-ITERATIONS FOR NON-LINEARITIES`` /
  ``MAXIMUM NUMBER OF ITERATIONS FOR ADVECTION SCHEMES``, ``H CLIPPING : NO``, and a
  raised k-epsilon solve budget (``MAXIMUM NUMBER OF ITERATIONS FOR K AND EPSILON``,
  config ``max_keps_iterations``). Set ``finite_volumes: true`` for the **HLLC
  finite-volume** kernel (``EQUATIONS : 'SAINT-VENANT FV'``, ``FINITE VOLUME SCHEME :
  5``): robust for transcritical flow and intrinsic wetting/drying, but **constant
  viscosity only**.
* **Turbulence** is auto-selected (``turbulence_model: auto``) from the channel cell
  size vs. the flow depth and the velocity guess: Smagorinski LES (4) when the mesh
  resolves >=80% of the TKE, k-epsilon (3) at moderate resolution, Spalart-Allmaras (6)
  on coarse meshes. The finite-volume kernel forces constant viscosity (model 1;
  ``VELOCITY DIFFUSIVITY`` sets it) - k-epsilon / Spalart-Allmaras need finite elements.
* ``FREE SURFACE GRADIENT COMPATIBILITY : 0.9`` strongly damps free-surface wiggles
  over steep bathymetry; ``CONTROL OF LIMITS`` clips H/U/V as a divergence guard;
  ``PRINTING CUMULATED FLOWRATES : YES`` writes the per-boundary fluxes the
  hotstart convergence check reads. The graphic printout is ``'U,V,S,B,H,M,Q,F'``,
  plus ``K,E`` (TKE + dissipation) for the k-epsilon model.
* **Time bound & convergence.** With the variable time step the steady run is capped
  by ``DURATION`` - the explicit ``hydrodynamics.duration`` (seconds), else the
  ``n_time_steps * time_step`` fallback, so the small CFL start step no longer shrinks
  the simulated time; ``NUMBER OF TIME STEPS`` does **not** terminate a variable-dt run. Convergence is judged afterwards from the **boundary-flux
  balance** (``initial_run.py``, :mod:`hydromate.sortie` +
  :mod:`hydromate.flux_convergence`), not by the solver. The **steady-state
  auto-stop** (``stop_if_steady``) is **off by default** and only honoured with a fixed
  time step: TELEMAC's ``STOP CRITERIA`` is an *absolute per-step* change, which with the
  tiny CFL dt false-fires during a slow transient (a still-filling reach) long before the
  fluxes balance - stopping the run far from steady state. Never on the unsteady run;
  TELEMAC-3D has no such keyword.
* **Dry start (default)** - the initial run starts dry except a thin water plug on
  the nodes near the inflow line (``dry_start_depth`` over ``dry_start_extent``), so
  the prescribed-Q inflow can establish (a fully dry bed makes TELEMAC's ``DEBIMP``
  abort) while the rest of the domain wets from the inflow. **Pre-wetting**
  (``prewet_depth``) is the alternative: it seeds the whole channel with a smooth,
  thalweg-following surface up front (used by the mesh-convergence study to skip the
  wetting front per mesh), and needs ``geodata.mesh_zones``.

Boundary conditions come from the :ref:`liquid-boundary lines <input-geodata>`:
inflow nodes get a prescribed discharge (the total reach Q split across inflow
boundaries by node count), outflow nodes a fixed prescribed elevation (the default)
or a water level from the rating curve / a free outflow per ``outflow_condition``.

The pipeline
------------

``hydromate`` runs five stages (see :doc:`codedocs`):

#. **DEM → ROI** (:mod:`hydromate.dem`) - reproject and clip the DEM(s) to the
   boundary.
#. **Mesh + bathymetry** (:mod:`hydromate.mesh`, :mod:`hydromate.selafin`) - a gmsh
   triangular mesh (flow-aligned and anisotropic in the channel - see `Meshing`_),
   the DEM interpolated onto the nodes, written as a TELEMAC geometry ``.slf`` with
   friction zones embedded as a per-node ``FRIC_ID`` variable.
#. **Boundary conditions** (:mod:`hydromate.boundary`) - classify the mesh contour
   against the liquid-boundary lines and write the ``.cli``.
#. **Steering + friction** (:mod:`hydromate.steering`) - the TELEMAC-2D ``.cas``
   (see `Numerics`_) and the zonal friction ``.tbl`` (and a GAIA ``.cas`` when
   morphodynamics is enabled).
#. **Calibration** (:mod:`hydromate.calibration`, :mod:`hydromate.ground_truth`) -
   compile the ground-truth sources into the tidy table, turn it into the
   calibration-points CSV, and emit a ready HydroBayesCal ``config_Telemac.py``.

.. toctree::
   :hidden:

   hbc
