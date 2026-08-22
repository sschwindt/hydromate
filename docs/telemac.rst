TELEMAC workflow
================

TELEMAC is the depth-averaged path and the one to run first: it answers the whole reach in hours, it is what the calibration is run on, and it is also the hotstart for both the TELEMAC-3D extension and the :doc:`OpenFOAM build <openfoam>`. Everything on this page assumes the case has been prepared and built as described in :doc:`preprocessing`.

.. code-block:: bash

   python cases/example-Inn/preprocessing.py            # build the case
   python cases/example-Inn/initial_run.py              # test-run + hotstart convergence
   python cases/example-Inn/mesh_convergence_study.py   # grid-independence study
   python cases/example-Inn/run_Bayes_cal.py            # Bayesian calibration

#. **Preprocessing / build** (``preprocessing.py``, or ``axqua <config>``) - clip the DEM(s), build the mesh and bathymetry, classify the liquid boundaries, and write the complete TELEMAC case (``geometry.slf``, ``boundaries.cli``, ``friction.tbl``, ``steady2d.cas``) plus the calibration CSV and the HydroBayesCal ``config_Telemac.py``. No solver is launched. This is also where everything that depends on the *geometry* is decided and logged, so it can be checked before any compute is spent: the initial condition (seeded at the normal-flow stage of real cross-sections), the outflow rating, and - for a :ref:`gain-lose reach <usage-gain-lose>` - the water table, the exchange faces it implies and the discharge they would carry.
#. **Initial run** (``initial_run.py``) - test-run the built case once to confirm it does not crash, and check that the boundary fluxes have reached mass balance. This concludes preprocessing.
#. **Mesh-convergence study** (``mesh_convergence_study.py``) - the grid-independence study, worth starting only **once the initial run has confirmed the model runs**. It rebuilds the case at a ladder of mesh resolutions and compares the results, and it pre-wets the channel per mesh so no run has to march a wetting front from the inflow.
#. **Optional extensions** - ``add3d.py`` (TELEMAC-3D sigma layers) then ``vertical_convergence_3d.py``; ``unsteady_run.py`` for a hydrograph.
#. **Calibration and validation** - hand the built case to HydroBayesCal (:doc:`hbc`).

The ``cases/example-Inn/`` scripts drive the worked Inn example from its ``case-config.yml``; copy the data-free ``cases/case-template/`` folder to start your own case.

.. _usage-initial-condition:

The initial condition
---------------------

The steady run starts either **dry** (the default: a thin water plug only at the inflow line, because a fully dry bed makes TELEMAC's ``DEBIMP`` abort at a prescribed-Q inflow) or **pre-wetted** (``initialization.prewet_depth``), which skips marching the wetting front from the inflow and is what the mesh-convergence study uses.

How the pre-wet surface is built matters more than it looks. With ``prewet_mode: normal-depth`` (the default) aXqua cuts a real cross-section every ``prewet_bin_spacing`` metres along the centerline and seeds the **stage that conveys the case discharge** through it, using the same Keulegan conveyance inversion that builds the outflow rating. The seeded surface then tracks the surface the run converges to.

``prewet_fill`` scales the depth above the thalweg and defaults to **0.70, deliberately below 1**. The asymmetry is the point: under-seeding is recoverable, because the flow refills a pool within seconds, whereas over-seeding is not. Water seeded above the converged surface has nowhere to go in a model with neither infiltration nor evaporation; it drains only where it can flow, and on flat ground over coarse gravel it stops as immobile film that survives to the end of the run. ``prewet_min_depth`` similarly leaves a node dry rather than laying down a feathered margin, since a seed film is exactly what stalls.

Whatever the mode, the **inflow plug is re-imposed after every filter**: none of them has any reason to keep the inflow cross-section wet, and ``DEBIMP`` aborts at t=0 without it. Check the result with the wetted-extent report described in :doc:`results`.

The initial run
---------------

``initial_run.py`` launches the built ``steady2d.cas`` **once** (no rebuild) to confirm the case runs, then checks the boundary-flux mass balance - the hotstart convergence check that :doc:`hbc` depends on.

* **Live output and progress bar.** The solver's listing is streamed to the terminal as the run marches, rather than captured silently, and a single-line progress bar tracks the **simulated time against the run's** ``DURATION``, the point the variable-time-step march reaches when no stop criterion fires. Because the CFL-adaptive step makes the *number* of time steps unknown in advance, progress is measured against that simulated-time cap, with the live iteration count shown alongside (:class:`axqua.progress.SolverProgress`, wired by :func:`axqua.run_solver_streaming`). **Every** solver launch in the workflow streams the same way: the per-mesh runs of the mesh-convergence study, the per-layer runs of the vertical convergence study, and the ``--run`` modes of ``add3d.py`` and ``unsteady_run.py``.
* **Core-count override.** Set the module-level ``NCSIZE`` at the top of ``initial_run.py`` to run this test on a different number of MPI processes than the ``telemac.n_processors`` chosen during preprocessing; leave it ``None`` to use the configured count. The command run is exactly ``telemac2d.py steady2d.cas --ncsize=<N> -s --nozip``, so the wrapper adds no compute overhead versus launching TELEMAC by hand.
* **Flux convergence and the generated hotstart case.** After the run, the fluxes are read from the ``.sortie`` listing and reported; once the imbalance is small enough for long enough, a ``hotstart2d.cas`` is written next to the steady case. The files this produces are described in :doc:`results`.
* **Where the water is.** A balanced flux budget says nothing about wetted *extent*, and the two are independent failure modes: a run can close its budget to 1e-4 and still show water standing where the reach has none. The wetted-extent and outlet-profile reports in :doc:`results` answer that question.

.. _numerics:

Numerics
--------

The steering (``.cas``) defaults are tuned for a real, steep, wetting/drying river reach. They are set in the ``hydrodynamics`` config block; the initial condition is in the ``initialization`` block.

* **Finite elements** (``finite_volumes: false``, the default) - the classic kernel, with an advection scheme, a preconditioned linear solver and an explicit tidal-flats treatment, written **compute-stable by default** to keep the wetting/drying steady march from exploding. The time step is **CFL-bound** on the sub-metre channel cells, so ``VARIABLE TIME-STEP : YES`` and ``DESIRED COURANT NUMBER : 0.30`` let TELEMAC adapt the step (``time_step: 0.25`` is only the conservative start step). The robustness knobs are ``IMPLICITATION FOR DEPTH/VELOCITY : 0.80`` (config ``implicitation``), ``DISCRETIZATIONS IN SPACE : 11;11`` (linear FE, required by the distributive advection scheme 14), ``NUMBER OF SUB-ITERATIONS FOR NON-LINEARITIES`` and ``MAXIMUM NUMBER OF ITERATIONS FOR ADVECTION SCHEMES``, ``H CLIPPING : NO``, and a raised k-epsilon solve budget (``MAXIMUM NUMBER OF ITERATIONS FOR K AND EPSILON``, config ``max_keps_iterations``). Set ``finite_volumes: true`` for the **HLLC finite-volume** kernel (``EQUATIONS : 'SAINT-VENANT FV'``, ``FINITE VOLUME SCHEME : 5``): robust for transcritical flow and intrinsic wetting/drying, but **constant viscosity only**.
* **Turbulence** is auto-selected (``turbulence_model: auto``) from the channel cell size versus the flow depth and the velocity guess: Smagorinski LES (4) when the mesh resolves at least 80% of the TKE, k-epsilon (3) at moderate resolution, Spalart-Allmaras (6) on coarse meshes. The finite-volume kernel forces constant viscosity (model 1, with ``VELOCITY DIFFUSIVITY`` setting it) - k-epsilon and Spalart-Allmaras need finite elements.
* ``FREE SURFACE GRADIENT COMPATIBILITY : 0.9`` strongly damps free-surface wiggles over steep bathymetry; ``CONTROL OF LIMITS`` clips H/U/V as a divergence guard; ``PRINTING CUMULATED FLOWRATES : YES`` writes the per-boundary fluxes the hotstart convergence check reads. The graphic printout is ``'U,V,S,B,H,M,Q,F'``, plus ``K,E`` (TKE and dissipation) for the k-epsilon model.
* **Time bound and convergence.** With the variable time step the steady run is capped by ``DURATION`` - the explicit ``hydrodynamics.duration`` in seconds, else the ``n_time_steps * time_step`` fallback, so the small CFL start step no longer shrinks the simulated time; ``NUMBER OF TIME STEPS`` does **not** terminate a variable-dt run. Convergence is judged afterwards from the **boundary-flux balance** (``initial_run.py``, :mod:`axqua.sortie` and :mod:`axqua.flux_convergence`), not by the solver. The **steady-state auto-stop** (``stop_if_steady``) is **off by default** and only honoured with a fixed time step: TELEMAC's ``STOP CRITERIA`` is an *absolute per-step* change, which with the tiny CFL dt false-fires during a slow transient - a still-filling reach - long before the fluxes balance, stopping the run far from steady state. It is never written for the unsteady run, and TELEMAC-3D has no such keyword.
* **Dry start (default)** - the initial run starts dry except a thin water plug on the nodes near the inflow line (``dry_start_depth`` over ``dry_start_extent``), so the prescribed-Q inflow can establish while the rest of the domain wets from the inflow. **Pre-wetting** (``prewet_depth``) is the alternative, and needs ``geodata.mesh_zones``.

Boundary conditions come from the :ref:`liquid-boundary lines <input-geodata>`: inflow nodes get a prescribed discharge (the total reach Q split across inflow boundaries by node count), outflow nodes a fixed prescribed elevation (the default), a water level from the rating curve, or a free outflow, per ``outflow_condition``.

Optional 3D extension
---------------------

A 3D simulation builds on the 2D one and is run **only after it**: it needs the 2D **hotstart** result (``r2d.slf`` from the initial run) and reuses the horizontal mesh whose resolution the mesh-convergence study has already validated.

#. **3D cases** (``add3d.py``) - write exactly **three** TELEMAC-3D steering files hotstarted from the 2D result (:func:`axqua.build_3d_cases`); the number of sigma layers and the turbulence model are inferred from ``r2d.slf`` and the fixed time step is sized for a Courant number of 0.6:

   * ``hotstart3d_hydrostatic.cas`` - ``NON-HYDROSTATIC VERSION : NO`` (the cheap hydrostatic solver), constant in-file Q/H, ~30k fixed steps with a short listing period: the steady **boundary-flux convergence check** (the sortie's ``FLUX BOUNDARY`` printouts show when in- and outflow balance in 3D).
   * ``hotstart3d_hydrodyn.cas`` - ``NON-HYDROSTATIC VERSION : YES``; the steady non-hydrostatic run with the same in-file prescribed Q (inflow) and H (outflow).
   * ``unsteady3d.cas`` - non-hydrostatic and hydrograph-driven: the time-variable inflow Q(t) and the stage-discharge outflow SL(t) come from the **same** ``LIQUID BOUNDARIES FILE`` as ``unsteady2d.cas``. It needs a *varying* ``boundaries.inflow`` series, and is skipped with a notice otherwise.

   ``--run [hydrostatic|hydrodyn|unsteady]`` also launches ``telemac3d.py`` on one of them; the default is the hydrostatic flux-convergence check.
#. **Vertical-layer convergence** (``vertical_convergence_3d.py``) - the number of vertical layers (``dz``) is a *new* discretization choice the 2D mesh-convergence study never covered, so it gets its **own** grid-independence study over the layer count, the 3D analogue of the mesh-convergence step.

.. code-block:: bash

   python cases/example-Inn/add3d.py                    # write (and --run) the 3D cases
   python cases/example-Inn/vertical_convergence_3d.py  # vertical-layer convergence

.. _usage-gain-lose:

Gain-lose reaches (flow through a porous body)
----------------------------------------------

Some reaches lose flow into a porous body - a gravel bar, an alluvial patch - and regain it downstream. A 2D depth-averaged model has no subsurface, so aXqua represents the underflow as an internal **withdrawal** where water infiltrates plus an **injection** where it resurfaces, generated into a TELEMAC ``USER_RAIN`` routine (a ``FORTRAN FILE`` that TELEMAC compiles at run time). Enable it by pointing ``gain_lose.zone`` at the porous body:

.. code-block:: yaml

   gain_lose:
     enabled: true
     zone: user-sources/geodata/porous-body.gpkg
     conductivity: 3.0e-4        # kf [m/s]
     water_table: phreatic

The generated routine is **depth-limited** - the withdrawal tapers to zero as a cell approaches ``min_depth`` and is additionally capped at half the water available that step - so a sink can never dry a cell. TELEMAC's own source terms carry no such guard, and an unguarded sink drying marginal cells spikes the velocities and pins the CFL-adaptive time step at a value from which the run never recovers. It is also **mass-exact**: whatever is withdrawn is reinjected in the same step, so no net sink appears in the boundary budget - which matters beyond tidiness, because a permanent sink ``S`` makes the steady budget read ``|Q_in| - |Q_out| = S`` and puts a floor under the relative flux imbalance, so no hotstart case would ever be written again.

**Where the exchange happens** is ``gain_lose.faces``:

``water-table`` (the default)
   The faces follow the physics and **nothing has to be drawn**. The body's saturated zone is bounded by the two channel levels it exchanges with, so its water table is known (:mod:`axqua.watertable` fits it as a plane, taking those levels from the channel surface at each end of the zone's reach extent). A node then **loses** where it is wet and its free surface stands above the table, and **gains** where the table stands above the bed. The classification runs *inside* the generated routine, so the faces move with the stage - a rising river widens the losing face, which a build-time mask cannot represent. Needs ``geodata.channel_centerline``. Prefer this: it is genuinely fuzzy where percolation begins and ends, so asking for the faces to be drawn asks for a number nobody has.

``lines``
   The exchange is pinned to the ``int-*`` lines of ``boundaries.liquid_boundaries``, each buffered to a strip. Pick this when the location is actually known - a surveyed seepage face, a spring line - or to reproduce a calibrated exchange.

**How big it is** is either ``conductivity`` or ``discharge``, and both work with either geometry. ``conductivity`` (``kf``) drives it by default through Green-Ampt's saturated limit ``f = kf (h + Lz + hf) / Lz`` at the local head, so the exchange *responds to water level*; ``discharge`` overrides that with a measured total, normalised over the losing face.

.. note::

   Riverbed ``kf`` is not a textbook constant - it spans 1e-9 to 1e-2 m/s with grain size and colmation, and varies within a single site (Calver, A., 2001, *Riverbed Permeabilities: Information from Pooled Data*, Ground Water 39(4), 546-553, `doi:10.1111/j.1745-6584.2001.tb02343.x <https://ngwa.onlinelibrary.wiley.com/doi/10.1111/j.1745-6584.2001.tb02343.x>`_). As a starting bracket: clean open-framework gravel 1e-2..1e-1, moderately colmated gravel 1e-4..1e-3, strongly colmated or silted 1e-7..1e-5. Because the plausible range is this wide, ``kf`` is exposed to HydroBayesCal as the calibration parameter ``POROUS ZONE kf (gain-lose)`` - pick a bracket from the reference and let the calibration find the value rather than assuming one.

The water table does one more thing worth knowing. With ``water_table: phreatic`` the pre-wet seeds any ground lying below it, so **closed depressions on the bar start full**. They can never fill otherwise: a hollow sitting above the channel is unreachable by surface flow, and the drainable-seed filter deliberately refuses to seed behind a rim. The patch drain likewise tapers to zero *at the table* rather than at an absolute depth, so it clears standing water off the bar top without emptying a pool that cuts below the saturated zone.

.. _morphodynamics:

Morphodynamics (GAIA)
---------------------

Sediment transport and bed evolution are handled by **GAIA**, coupled to the hydrodynamic run. Because a flood wave is what actually reworks the bed, GAIA is most useful on the **unsteady** runs: enabling ``morphodynamics`` couples it to the hydrograph-driven ``unsteady2d.cas`` **and** its 3D twin ``unsteady3d.cas``, as well as to the steady case, sharing a single generated GAIA steering file. The coupling adds ``COUPLING WITH : 'GAIA'``, ``GAIA STEERING FILE`` and ``COUPLING PERIOD FOR GAIA`` to the driver ``.cas`` and writes a GAIA ``.cas`` next to it.

Turn it on in the config (or via the ``GAIA_*`` toggles at the top of ``unsteady_run.py``) and declare the sediment and bed processes:

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

The generated GAIA ``.cas`` references the **same geometry and boundary files** as the coupled run (GAIA needs both even when coupled), enables the chosen transport modes and emits the bed-process keywords above. Sediment-transport parameters such as Shields numbers and class diameters are also the natural :doc:`calibration <hbc>` targets, perturbed by HydroBayesCal through the ``gaia<KEYWORD>`` parameters. Run a morphodynamic case with the unsteady script:

.. code-block:: bash

   python cases/example-Inn/unsteady_run.py --run   # GAIA_ENABLED=True + MODE_3D toggles the 2D/3D run
