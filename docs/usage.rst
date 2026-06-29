Usage
=====

``hydromate`` is driven by a single YAML configuration file pointing at your
:doc:`input files <input_files>`. From there the workflow is a fixed, ordered
chain - each step only makes sense once the previous one has succeeded:

#. **Prepare the input files** - the :ref:`geodata <input-geodata>`, the
   :ref:`ground truth <input-ground-truth>`, and the :ref:`config YAML <input-config>`.
#. **Preprocessing / build** (``preprocessing.py``, or ``hydromate <config>``) -
   clip the DEM(s), build the mesh + bathymetry, classify the liquid boundaries,
   and write the complete TELEMAC case (``geometry.slf``, ``boundaries.cli``,
   ``friction.tbl``, ``steady2d.cas``) plus the calibration CSV and HydroBayesCal
   ``config_Telemac.py``. No solver is launched.
#. **Initial run** (``initial_run.py``) - test-run the built case once to confirm
   it does not crash, and check that the boundary fluxes have reached mass balance
   (the hotstart convergence check). This concludes preprocessing.
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

Optional 3D extension (after the 2D path)
-----------------------------------------

A 3D simulation builds on the 2D one and is run **only after it**: it needs the 2D
**hotstart** result (``r2d.slf`` from the initial run) and reuses the horizontal mesh
whose resolution the mesh-convergence study has already validated.

#. **3D case** (``add3d.py``) - write a non-hydrostatic TELEMAC-3D case
   (``<case-name>3d.cas``) hotstarted from the 2D result; the number of sigma layers
   and the turbulence model are inferred from ``r2d.slf`` and the time step is sized
   for a Courant number of 0.6 (``--run`` also launches ``telemac3d.py``).
#. **Vertical-layer convergence** (``vertical_convergence_3d.py``) - the number of
   vertical layers (``dz``) is a *new* discretization choice the 2D mesh-convergence
   study never covered, so it gets its **own** grid-independence study over the layer
   count, the 3D analogue of the mesh-convergence step.

.. code-block:: bash

   python cases/example-Inn/add3d.py                    # write (and --run) the 3D case
   python cases/example-Inn/vertical_convergence_3d.py  # vertical-layer convergence

The ``cases/example-Inn/`` scripts drive the worked Inn example from its
``case-config.yml``; copy the data-free ``cases/case-template/`` folder to start
your own case.

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
automatically to ``inputs.dem_initial`` (and ``inputs.dem_target``) during a build.

.. _meshing:

Meshing
-------

The mesh is generated with `gmsh <https://gmsh.info>`_. Two strategies are chosen
automatically.

**Anisotropic, flow-aligned (preferred).** When you provide ``inputs.mesh_zones``
and ``inputs.channel_centerline``, the channel is meshed with triangles
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
``inputs.breaklines`` and to ``region_sizes`` near MATID region points.

.. _numerics:

Numerics
--------

The steering (``.cas``) defaults are tuned for a real, steep, wetting/drying river
reach (set in the ``hydrodynamics`` config block):

* **Finite volumes** (``finite_volumes: true``, the default) - ``EQUATIONS :
  'SAINT-VENANT FV'`` with the **HLLC** scheme (``FINITE VOLUME SCHEME : 5``,
  2nd-order in space). The finite-volume kernel is robust for transcritical flow
  and handles wetting/drying intrinsically (no tidal-flat treatment needed). The
  scheme is explicit, so the time step is **CFL-bound**: ``VARIABLE TIME-STEP : YES``
  + ``DESIRED COURANT NUMBER : 0.9`` let TELEMAC adapt the step to the (sub-metre)
  channel cells. Set ``finite_volumes: false`` for the classic finite-element
  kernel (which then uses an advection scheme, a preconditioned linear solver and
  an explicit tidal-flats treatment).
* **Turbulence.** Finite volumes accept **only constant viscosity**
  (``TURBULENCE MODEL : 1``); ``VELOCITY DIFFUSIVITY`` sets the eddy viscosity.
  k-epsilon (3) and Spalart-Allmaras (6) are rejected by the FV kernel - to use
  them, switch to finite elements (``finite_volumes: false``).
* ``FREE SURFACE GRADIENT COMPATIBILITY : 0.1`` damps free-surface wiggles over
  steep bathymetry; ``CONTROL OF LIMITS`` clips H/U/V as a divergence guard;
  ``PRINTING CUMULATED FLOWRATES : YES`` writes the per-boundary fluxes the
  hotstart convergence check reads.
* **Steady-state auto-stop** (``stop_if_steady``, default on) - the steady run emits
  ``STOP IF A STEADY STATE IS REACHED : YES`` with ``STOP CRITERIA`` (the relative
  change thresholds for ``(U,V)``, ``H`` and tracers, default ``1.E-4``), so it halts
  as soon as the solution stops changing rather than running all ``n_time_steps``.
  Omitted for the unsteady hydrograph run; TELEMAC-3D has no such keyword.
* **Dry start (default)** - the initial run starts dry except a thin water plug on
  the nodes near the inflow line (``dry_start_depth`` over ``dry_start_extent``), so
  the prescribed-Q inflow can establish (a fully dry bed makes TELEMAC's ``DEBIMP``
  abort) while the rest of the domain wets from the inflow. **Pre-wetting**
  (``prewet_depth``) is the alternative: it seeds the whole channel with a smooth,
  thalweg-following surface up front (used by the mesh-convergence study to skip the
  wetting front per mesh), and needs ``inputs.mesh_zones``.

Boundary conditions come from the :ref:`liquid-boundary lines <input-geodata>`:
inflow nodes get a prescribed discharge (the total reach Q split across inflow
boundaries by node count), outflow nodes a prescribed water level from the rating
curve (or a fixed elevation / free outflow per ``outflow_condition``).

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
