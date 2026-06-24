Usage
=====

``hydromate`` is driven by a single YAML configuration file. You can write it in a text editor and drive the build from the command line, or edit it in a browser with the optional graphical editor — both produce the same YAML. The workflow is: write a config, build the case, then calibrate it with HydroBayesCal.

The command line
----------------

.. code-block:: bash

   hydromate config/inn.yml --check     # validate the config and TELEMAC env only
   hydromate config/inn.yml             # build the full case
   hydromate config/inn.yml --dry-run   # build, then run the solver once to validate
   hydromate config/inn.yml -v          # verbose (per-stage) logging

Useful flags:

``--check``
    Load and validate the configuration (paths exist, required inputs present, TELEMAC ``pysource`` resolvable) and exit without building.
``--no-validate-env``
    Skip checking that the TELEMAC environment can be sourced (useful when only producing files on a machine without TELEMAC).
``--dry-run``
    After building, launch the configured solver once to confirm the case is accepted by TELEMAC.

Graphical configuration editor
------------------------------

An optional browser-based editor (built with Streamlit) renders the whole configuration as a form, so you can fill in the settings without hand-editing YAML. Install the ``gui`` extra and launch it:

.. code-block:: bash

   pip install -e ".[gui]"
   hydromate-gui

This opens a local app in your browser (nothing leaves your machine). The form mirrors the configuration sections (project, TELEMAC, inputs, mesh, friction, hydrodynamics, calibration); friction zones and calibration parameters are edited as tables. From the editor you can load an existing YAML, preview and download the generated YAML, save it to a path, and run validation (``--check``) or a full build directly — the same actions as the command line. Validation and build operate on the saved file, so its relative input paths resolve correctly.

Clipping a raster to the region of interest
-------------------------------------------

The ``clip`` subcommand crops a single raster (e.g. a DEM) to a region-of-interest polygon without needing a full configuration — handy for preparing inputs. The raster is cropped in its own grid and resolution; if it has no embedded CRS, the boundary's CRS is assumed and written onto the output.

.. code-block:: bash

   hydromate clip path/to/dem.tif -b path/to/roi.gpkg -o path/to/dem-roi-clip.tif

For example, to crop two DEMs in a ``geodata`` folder to the same ROI:

.. code-block:: bash

   hydromate clip geodata/dem-2020.tif       -b geodata/roi.gpkg -o geodata/dem-2020-roi-clip.tif
   hydromate clip geodata/DEM-2025-20cm.tif  -b geodata/roi.gpkg -o geodata/dem-2025-roi-clip.tif

Options: ``--epsg <code>`` reprojects the raster to that EPSG before clipping; ``--all-touched`` keeps pixels touched by the polygon edge (the default keeps pixels whose centre is inside the polygon). The same clipping is applied automatically to ``inputs.dem_initial`` (and ``inputs.dem_target``) during a full build.

Worked example scripts
----------------------

The repository ships two example scripts under ``example-Inn/`` that drive the workflow from a case configuration and are a good starting point to copy for your own case. ``preprocessing.py`` loads ``config/<case>.yml`` and clips the configured DEM(s) to the ROI, generates the mesh and stores the bare geometry as ``mesh-raw.slf``, interpolates DEM elevations onto the nodes (rounded to 4 decimals) and stores ``mesh-elevations.slf``, then compiles the ground-truth calibration table. ``run2postprocessing.py`` then builds the full TELEMAC case from the same configuration and points to the HydroBayesCal calibration.

.. code-block:: bash

   python example-Inn/preprocessing.py
   python example-Inn/run2postprocessing.py

Inputs you provide
------------------

* an **initial ROI DEM** (GeoTIFF) and, optionally, a **target DEM** for morphodynamics;
* a **boundary** polygon delineating the region of interest / maximum wetted extent;
* **liquid-boundary** lines tagged inflow / outflow;
* **inflow** discharge (a single value or a time series);
* optionally a **stage-discharge** rating curve for the outlet;
* optionally **mesh zones** (polygons tagged ``channel``/``floodplain``) and a **channel centerline** to drive the flow-aligned anisotropic mesh — see `Meshing`_;
* optionally **breaklines** and **region/MATID points** to control the mesh and friction zones;
* optionally **ground-truth measurements** (water depth, flow velocity, …) that become the calibration data — see `Ground-truth measurements`_ below;
* optionally a **DEM-of-Difference** (initial − target) as topographic-change calibration data.

All inputs are declared by path in the configuration; relative paths are resolved against the configuration file's own directory, and any input in a different coordinate system is reprojected to the project CRS on ingest.

Ground-truth measurements
-------------------------

The calibration data is supplied as a **tidy, multi-tab table** (an ``.xlsx`` workbook). Each tab is one *category* of measurement (``hydraulics``, ``sediment``, …); within a tab the **first three columns are the coordinates** ``x``, ``y``, ``z`` (in the project CRS), and every **following column is a measured quantity** at that point. Column headers are matched case-insensitively and common aliases are accepted (e.g. ``easting``/``northing`` for ``x``/``y``, ``depth`` for ``h``, ``vx``/``vy``/``vz`` for ``u``/``v``/``w``).

For FlowTracker-style hydraulics a row carries the velocity components ``u``, ``v``, ``w`` (m/s), their per-component errors ``u_err``, ``v_err``, ``w_err`` (m/s), and the water depth ``h`` (m). **Not every dataset has every column** — provide only what you measured. The calibration stage derives each requested ``calibration_quantity`` from whatever columns are present (``WATER DEPTH`` from ``h``; ``SCALAR VELOCITY`` from the velocity components, with its error propagated from ``u_err``/``v_err``/``w_err`` when given) and falls back to the configured ``measurement_error`` fraction where no measured error exists. A quantity you cannot supply the columns for is reported as an error rather than silently skipped.

There are two ways to provide this table:

#. **Author it yourself** (the general case): create the ``.xlsx`` with the structure above and point ``inputs.measurements`` at it.
#. **Let hydromate compile it** from raw field exports (used by the Inn showcase): declare the raw sources under ``inputs.ground_truth`` and the tidy table is generated for you (written to ``work_dir/ground-truth.xlsx`` unless ``inputs.measurements`` sets an explicit output path). Each source names a ``category``, an adapter ``kind``, the ``values`` file and — when the values file has no coordinates — a ``positions`` point layer to join on (by ``join_key``), which is reprojected to the project CRS:

   .. code-block:: yaml

      inputs:
        ground_truth:
          - { category: hydraulics, kind: flowtracker, join_key: ID,
              values: ../ground-truth/hydraulics/FlowTracker2-day1.xlsx,
              positions: ../geodata/flowtracker2/dgps-day1.shp }

   The ``flowtracker`` adapter reads a SonTek FlowTracker2 ``.ft.sum`` export and joins each measurement vertical (by ``ID``) to its surveyed position; the ``points`` adapter reads a point layer (or CSV) that already carries the quantity columns. Sources sharing a ``category`` are concatenated into one tab.

Meshing
-------

The mesh is generated with `gmsh <https://gmsh.info>`_. Two strategies are chosen automatically.

**Anisotropic, flow-aligned (preferred).** When you provide ``inputs.mesh_zones`` (polygons each tagged in a ``Zone Name`` attribute) and ``inputs.channel_centerline`` (a line), the channel is meshed with triangles **elongated along the centerline** and the floodplain with **near-equilateral** triangles, blended smoothly between the two. This keeps cells aligned with the main flow — reducing numerical diffusion and cross-flow artifacts — while staying economical on the overbank. Zones whose ``Zone Name`` *contains* ``channel`` (case-insensitive) are treated as channel; those containing ``floodplain`` as floodplain. The behaviour is controlled by the ``mesh`` block:

.. code-block:: yaml

   inputs:
     mesh_zones: ../geodata/mesh-zones.gpkg            # polygons with a 'Zone Name'
     channel_centerline: ../geodata/channel-centerline.gpkg
   mesh:
     channel_size: 0.5          # cross-channel target edge length (m)
     floodplain_size: 1.5       # floodplain target edge length (m)
     growth_ratio: 1.2          # max edge-size growth per element, channel -> floodplain
     channel_anisotropy: 4.0    # along-flow / cross-flow edge-length ratio in the channel
     # zone_name_field: "Zone Name"   # the attribute naming each zone (default)

In the channel the cross-channel edge length is ``channel_size`` and the along-flow edge is stretched by ``channel_anisotropy`` (so ``0.5 m`` across and ``~2 m`` along, by default). These are *targets*: the mesh is built from a metric-tensor background field with gmsh's BAMG algorithm, which approximates the requested sizes and anisotropy (the realised cross size and elongation are typically a little gentler than the targets, and ``growth_ratio`` deliberately relaxes them near the banks). Push ``channel_anisotropy`` up for stronger elongation; raise the sizes for a quicker, coarser mesh. The defaults yield a high-resolution mesh — for a ~0.3 km² reach that is on the order of half a million elements — so increase the sizes while iterating.

**Isotropic fallback.** With no ``mesh_zones``/``channel_centerline``, the mesh uses ``default_size`` everywhere, refined to ``breakline_size`` along ``inputs.breaklines`` and to ``region_sizes`` near MATID region points.

The pipeline
------------

``hydromate`` runs five stages (see :doc:`codedocs`):

#. **DEM → ROI** (:mod:`hydromate.dem`) — reproject and clip the DEM(s) to the boundary.
#. **Mesh + bathymetry** (:mod:`hydromate.mesh`, :mod:`hydromate.selafin`) — a gmsh triangular mesh from the boundary and breaklines (flow-aligned and anisotropic in the channel — see `Meshing`_), the DEM interpolated onto the nodes, written as a TELEMAC geometry ``.slf`` with friction zones embedded as a per-node ``FRIC_ID`` variable.
#. **Boundary conditions** (:mod:`hydromate.boundary`) — classify the mesh contour against the liquid-boundary lines and write the ``.cli``.
#. **Steering + friction** (:mod:`hydromate.steering`) — the TELEMAC-2D ``.cas`` and the zonal friction ``.tbl`` (and a GAIA ``.cas`` when morphodynamics is enabled).
#. **Calibration** (:mod:`hydromate.calibration`, :mod:`hydromate.ground_truth`) — compile the ground-truth sources into the tidy table, turn it into the calibration-points CSV, and emit a ready HydroBayesCal ``config_Telemac.py``.

Configuration reference
-----------------------

A configuration file has these top-level sections:

``project``
    case name, ``crs_epsg`` (project coordinate system), and the output directories (``work_dir``, ``model_dir``, ``results_dir``).
``telemac``
    ``pysource`` (the TELEMAC environment script), ``solver`` and ``n_processors``.
``inputs``
    all input data paths (DEM(s), boundary, liquid boundaries, inflow, optional mesh zones + channel centerline, breaklines, region points, stage-discharge, ground truth).
``mesh``
    the anisotropic channel/floodplain edge lengths, ``growth_ratio`` and ``channel_anisotropy`` (see `Meshing`_), or the isotropic fallback's ``default_size``/``breakline_size``/``region_sizes``.
``friction``
    the default friction law/coefficient and one zone per MATID.
``hydrodynamics``
    steady/unsteady regime, time stepping, turbulence, and the prescribed boundary values.
``morphodynamics`` *(optional)*
    enable GAIA and declare sediment classes.
``calibration``
    the calibration and extraction quantities, the calibration parameters with their ranges, and the BAL sampling settings forwarded to HydroBayesCal.

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
     liquid_boundaries: ../geodata/liquid-boundaries.shp
     inflow: ../data/inflow.csv
   hydrodynamics:
     regime: steady
     prescribed_elevation: 371.33
   calibration:
     calibration_quantities: ["WATER DEPTH"]
     parameters:
       - { name: zone1, min: 0.02, max: 0.04 }

Calibration parameter naming
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Parameter names follow HydroBayesCal's prefix convention:

==========================================  ====================================
Name                                        Target
==========================================  ====================================
``zone<MATID>``                             bed-friction coefficient of a zone
``gaiaCLASSES SHIELDS PARAMETERS <n>``      GAIA critical Shields, sediment ``n``
``vg_zone<MATID>-<p>``                      vegetation friction parameter
any literal TELEMAC keyword                 written straight into the ``.cas``
==========================================  ====================================

Calibrating the case
--------------------

The build produces a ``config_Telemac.py`` in the model directory. Calibrate it
with HydroBayesCal:

.. code-block:: bash

   cd case/simulation
   python /path/to/hydrobayescal/bal_telemac.py --config config_Telemac.py
