Input Files
===========

``hydromate`` is driven by a single YAML **configuration file** that points, by
path, at the input data. There are three kinds of input:

#. **Geodata** - the rasters and vector layers describing the terrain, the model
   boundaries, the mesh/friction zones and the measurement positions;
#. the **simulation config** - the YAML itself (optionally edited in the graphical
   configurator);
#. **ground truth** - the field measurements that become calibration / validation
   targets.

All paths in the config are resolved **relative to the configuration file's own
directory**. Every vector/raster layer may be in any coordinate system: it is
**reprojected to the project CRS** (``project.crs_epsg``, metres) on ingest. Keep
raw inputs immutable - produced artifacts are written under ``tm-simulation/``.

.. _input-geodata:

Geodata
-------

The geodata layers and the **attribute fields** ``hydromate`` reads from them.
Field names are matched **case-insensitively**; vector layers may be GeoPackage
(``.gpkg``), Shapefile (``.shp``) or anything GDAL/OGR reads.

.. list-table::
   :header-rows: 1
   :widths: 22 12 30 36

   * - Config key
     - Geometry
     - Required attribute field(s)
     - Notes
   * - ``inputs.dem_initial`` *(required)*
     - raster
     - --
     - baseline terrain (GeoTIFF). ``inputs.dem_target`` (optional) is a second
       DEM that enables the morphodynamic / DEM-of-Difference path.
   * - ``inputs.boundary`` *(required)*
     - polygon
     - --
     - the ROI / maximum-wetted-extent. A single closed polygon.
   * - ``inputs.liquid_boundaries`` *(required for a build)*
     - lines
     - a *type* field tagging each line ``inflow`` or ``outflow``
     - the field name may be ``Type (inflow/outflow)`` (the Inn layer's typo
       ``Type (inflow/outlfow)`` is also detected) - any column whose name mentions
       *type / kind / inflow / outflow / stringdef*. Cell **values** must contain
       ``inflow`` or ``outflow``. The lines MUST coincide with the outer bounds of
       the mesh zones so the contour nodes fall on them. Several of each are allowed.
   * - ``inputs.mesh_zones`` *(optional)*
     - polygons
     - ``Zone Name`` and ``Max Edge Length (m)``
     - ``Zone Name`` is classified by substring into ``channel`` / ``floodplain`` /
       ``refinement``; ``Max Edge Length (m)`` (a double; decimal point **or**
       German comma accepted) is the target edge length per polygon. Drives the
       anisotropic mesh - see :ref:`Meshing <meshing>`.
   * - ``inputs.channel_centerline`` *(optional)*
     - line
     - --
     - the line the channel cells are elongated along (needed for the anisotropic
       mesh, and for the pre-wetting longitudinal profile).
   * - ``inputs.roughness_zones`` *(optional)*
     - polygons
     - ``Zone ID`` (integer)
     - each polygon's integer ``Zone ID`` becomes the per-node ``FRIC_ID``; paired
       with ``inputs.roughness_table``.
   * - ``inputs.region_points`` *(optional)*
     - points
     - ``MATID`` (or ``FRIC_ID`` / ``MAT_ID``), integer
     - seed points for the older MATID friction/zone scheme (isotropic mesh path).
   * - ``inputs.breaklines`` *(optional)*
     - lines
     - --
     - internal constraint lines for the isotropic-fallback mesh.

Tabular geodata (CSV):

``inputs.inflow`` *(required for a build)*
    upstream discharge. Either a **generic CSV** with a discharge column
    (``value`` / ``q`` / ``discharge`` / ``flow``), or the Bavarian-LfU export
    format (``;`` separator, ``,`` decimal, metadata header, ``datetime;value``
    rows). A single value / row is enough for a steady run.
``inputs.roughness_table`` *(optional)*
    CSV ``zone_id,ks`` mapping each roughness ``Zone ID`` to a roughness value
    (e.g. a Nikuradse :math:`k_s` in metres).
``inputs.stage_discharge`` *(optional)*
    the outlet rating curve as a ``Q,WSE`` CSV (discharge [m³/s], water-surface
    elevation [m]); extra rows are interpolated. One pair at the steady discharge
    suffices. Auto-synthesised from a Manning/Strickler value + channel geometry by
    ``hydromate rating`` (or ``preprocessing.py``) when absent.

Measurement **positions** (point layers joined to the ground-truth value files by
``ID`` / row order) also live in geodata and are reprojected on ingest - see
:ref:`Ground truth <input-ground-truth>`.

Drawing the vector layers (QGIS)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The mesh zones, roughness zones, liquid boundaries and channel centerline are
digitised by hand in a GIS. They must line up: the liquid-boundary lines span the
inflow / outflow cross-sections and coincide with the outer bounds of the mesh
zones, and the centerline runs down the channel.

.. figure:: img/inflow-bc.jpg
   :alt: Mesh zones, roughness zones, liquid boundary and channel centerline at the Inn model inlet, over a hillshade
   :width: 100%

   The inlet of the Inn model: the mesh-zone and roughness-zone polygons, the
   liquid-boundary line and the channel centerline drawn over a **hillshade** of
   the DEM. The liquid-boundary line spans the inflow cross-section, the zone
   polygons meet along it, and the centerline runs down the channel.

.. tip:: Hillshade background for digitising

   A **hillshade raster** brings out the channel banks, bars and structures, which
   makes tracing the **channel centerline** (and digitising the mesh / roughness
   zones and liquid boundaries) far easier. Create one from the DEM in QGIS:
   *Processing Toolbox → GDAL → Raster analysis → Hillshade*, with **Azimuth 315,
   Altitude 45, Z factor 1**, then load it as the background under your vector
   layers (as in the figure above).

.. tip:: Carving ``refinement`` zones out of a larger zone (QGIS *Add Ring*)

   A local ``refinement`` polygon usually sits inside a larger ``channel`` /
   ``floodplain`` polygon. The cleanest way to nest it is to cut a matching hole
   (an interior ring) in the larger polygon so the two do not overlap:

   #. Make sure ``mesh-zones.gpkg`` is in **edit mode**.
   #. Select the large polygon (e.g. from the attribute table).
   #. Activate the **Add Ring** tool (first enable it via *View → Toolbars →
      Advanced Digitizing Toolbar*). The Add Ring tool creates an interior ring
      inside an existing polygon, i.e. it cuts a hole from that polygon; the hole
      must lie inside the polygon.
   #. Turn on **snapping / tracing** if you want to follow the boundary of the
      small (refinement) polygon exactly.
   #. Trace around the first small polygon, then **right-click** to finish the ring.
   #. Repeat for every small polygon, then **save edits**.

   Each refinement polygon itself carries a ``Zone Name`` containing ``refinement``
   and its own ``Max Edge Length (m)``.

.. _input-config:

Simulation config
-----------------

The configuration is a single YAML file with these top-level sections:

``project``
    case ``name``, ``crs_epsg`` (project coordinate system, metric), and the
    per-phase output directories under ``tm-simulation/`` (``preprocessing_dir``,
    ``model_dir``, ``postprocessing_dir``, ``calibration_dir``).
``telemac``
    ``pysource`` (the TELEMAC environment script, *sourced* - not imported -
    whenever the solver/SELAFIN tooling is needed), ``solver`` and ``n_processors``.
``inputs``
    all input data paths (the :ref:`Geodata <input-geodata>` above plus the
    :ref:`ground-truth <input-ground-truth>` sources).
``mesh``
    the per-zone (channel / floodplain / refinement) fallback edge lengths,
    ``growth_ratio`` and ``channel_anisotropy`` (see :ref:`Meshing <meshing>`), or
    the isotropic fallback's ``default_size`` / ``breakline_size`` / ``region_sizes``.
``friction``
    the default friction law/coefficient and one zone per MATID (when the MATID
    scheme is used).
``hydrodynamics``
    steady/unsteady regime, time stepping, the **numerics** (finite volumes vs
    finite elements, turbulence), the outflow-boundary type and prescribed values,
    and optional pre-wetting. See :ref:`Numerics <numerics>`.
``morphodynamics`` *(optional)*
    enable GAIA and declare sediment classes.
``calibration``
    the calibration and extraction quantities, the calibration parameters with
    their ranges, and the sampling settings forwarded to HydroBayesCal.

A minimal example:

.. code-block:: yaml

   project:
     name: inn
     crs_epsg: 25832
   telemac:
     pysource: /home/user/opt/telemac/configs/pysource.gfortran.sh
     solver: telemac2d
     n_processors: 4
   inputs:
     dem_initial: ../geodata/dem.tif
     boundary: ../geodata/roi.gpkg
     liquid_boundaries: ../geodata/liquid-boundaries.gpkg
     inflow: ../data/inflow.csv
   hydrodynamics:
     regime: steady
     finite_volumes: true        # HLLC finite volumes (robust); false -> finite elements
   calibration:
     calibration_quantities: ["WATER DEPTH"]
     parameters:
       - { name: zone1, min: 0.05, max: 0.50 }

The full, commented reference is ``cases/case-template/case-config.yml`` (copy the
whole ``cases/case-template/`` folder to start a new case).

Calibration parameter naming
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Parameter names follow HydroBayesCal's prefix convention:

==========================================  ====================================
Name                                        Target
==========================================  ====================================
``zone<N>``                                 bed-friction coefficient of zone ``N``
``gaiaCLASSES SHIELDS PARAMETERS <n>``      GAIA critical Shields, sediment ``n``
``vg_zone<N>-<p>``                          vegetation friction parameter
any literal TELEMAC keyword                 written straight into the ``.cas``
==========================================  ====================================

Graphical configurator
~~~~~~~~~~~~~~~~~~~~~~~~

Instead of hand-editing YAML you can fill in the configuration as a browser form
(built with Streamlit). Install the ``gui`` extra and launch it with the
``hydromate-gui`` console script:

.. code-block:: bash

   pip install -e ".[gui]"
   hydromate-gui                      # opens a local app in your browser
   # hydromate-gui --server.port 8600 # extra arguments are forwarded to Streamlit

This opens a local app in your browser (nothing leaves your machine). The form has
one tab per configuration section - **Project, TELEMAC, Inputs, Mesh, Friction,
Hydrodynamics, Morphodynamics, Calibration** - mirroring the dataclass schema (the
anisotropic mesh sizes, the finite-element / ``turbulence_model: auto`` numerics,
the ``outflow_condition`` and pre-wetting, the roughness/MATID friction, the GAIA
block), plus a **Workflow** tab summarising the steps. Friction zones, calibration
parameters, ground-truth sources and sediment classes are edited as tables.

You can load an existing YAML, preview and download the generated YAML, save it to a
path, and run **Validate** (``--check``) or **Build** directly - the same actions as
the command line, operating on the saved file so its relative input paths resolve.
The **Build** button runs the case build (``hydromate <config>``), i.e. workflow
**step 1**; the later steps (``initial_run.py`` test run, the mesh-convergence study,
HydroBayesCal, and the optional 3D extension) are run from the per-case scripts - see
:doc:`usage`.

.. _input-ground-truth:

Ground truth
------------

Field measurements become the **calibration** (and validation) targets. They are
supplied as a **tidy, multi-tab table** (an ``.xlsx`` workbook): each tab is one
*category* (``hydraulics``, ``sediment``, …); within a tab the **first three
columns are the coordinates** ``x``, ``y``, ``z`` (in the project CRS) and every
**following column is a measured quantity** at that point. Column headers are
matched case-insensitively with common aliases (``easting`` / ``northing`` for
``x`` / ``y``, ``depth`` for ``h``, ``vx`` / ``vy`` / ``vz`` for ``u`` / ``v`` /
``w``).

For FlowTracker-style hydraulics a row carries velocity components ``u``, ``v``,
``w`` (m/s), their per-component errors ``u_err``, ``v_err``, ``w_err`` (m/s), and
the water depth ``h`` (m). **Not every dataset has every column** - provide only
what you measured. The calibration stage derives each requested
``calibration_quantity`` from whatever columns are present (``WATER DEPTH`` from
``h``; ``SCALAR VELOCITY`` from the velocity components, with its error propagated
from ``u_err`` / ``v_err`` / ``w_err`` when given) and falls back to the configured
``measurement_error`` fraction where no measured error exists. A quantity whose
columns are missing is reported as an error rather than silently skipped.

There are two ways to provide the table:

#. **Author it yourself** (the general case): create the ``.xlsx`` with the
   structure above and point ``inputs.measurements`` at it.
#. **Let hydromate compile it** from raw field exports (the Inn showcase): declare
   the raw sources under ``inputs.ground_truth`` and the tidy table is generated
   for you (written to ``preprocessing/ground-truth.xlsx`` unless
   ``inputs.measurements`` sets an explicit output path). Each source names a
   ``category``, an adapter ``kind``, the ``values`` file and - when the values
   file has no coordinates - a ``positions`` point layer to join on (by
   ``join_key``), reprojected to the project CRS:

   .. code-block:: yaml

      inputs:
        ground_truth:
          - { category: hydraulics, kind: flowtracker, join_key: ID,
              values: ../ground-truth/hydraulics/FlowTracker2-day1.xlsx,
              positions: ../geodata/flowtracker2/dgps-day1.shp }

   The ``flowtracker`` adapter reads a SonTek FlowTracker2 ``.ft.sum`` export and
   joins each measurement vertical (by ``ID``) to its surveyed position; the
   ``points`` adapter reads a point layer (or CSV) that already carries the quantity
   columns. Sources sharing a ``category`` are concatenated into one tab.
