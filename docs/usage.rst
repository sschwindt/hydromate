Usage
=====

``hydromate`` is driven by a single YAML configuration file pointing at your
:doc:`input files <input_files>`. From there the workflow is a fixed, ordered
chain - each step only makes sense once the previous one has succeeded:

#. **Prepare the input files** - the :ref:`geodata <input-geodata>`, the
   :ref:`ground truth <input-ground-truth>` (generate the
   :ref:`calibration-target template <input-target-template>` with ``hydromate
   targets <config>`` and fill it in), and the :ref:`config YAML <input-config>`.
#. **Preprocessing / build** (``preprocessing.py``, or ``hydromate <config>``) -
   clip the DEM(s), build the mesh + bathymetry, classify the liquid boundaries,
   and write the complete TELEMAC case (``geometry.slf``, ``boundaries.cli``,
   ``friction.tbl``, ``steady2d.cas``) plus the calibration CSV and HydroBayesCal
   ``config_Telemac.py``. No solver is launched.
#. **Initial run** (``initial_run.py``) - test-run the built case once to confirm
   it does not crash, and check that the boundary fluxes have reached mass balance
   (the hotstart convergence check). The solver's output streams live with a
   progress bar (see `The initial run`_). This concludes preprocessing.
#. **Mesh-convergence study** (``mesh_convergence_study.py``) - the
   grid-independence study. It runs the case on five meshes, so it is only worth
   starting **once the initial run has confirmed the model runs**.
#. **Calibration & validation** - hand the built case to HydroBayesCal
   (:doc:`hbc`).

.. code-block:: bash

   python cases/example-Inn/preprocessing.py            # build the case
   python cases/example-Inn/initial_run.py              # test-run + hotstart convergence
   python cases/example-Inn/mesh_convergence_study.py   # grid-independence study

Prefer a form to hand-editing the YAML? Launch the browser-based configuration
editor with ``hydromate-gui`` (see :ref:`the graphical configurator <input-config>`);
its **Build** button is the same build step as above.

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
  :func:`hydromate.analyze_flux_convergence` (delegating to pythomac) reads the
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
  balance** (``initial_run.py`` / pythomac), not by the solver. The **steady-state
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
