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

The repository ships two example scripts under ``example-Inn/`` that drive the workflow from a case configuration and are a good starting point to copy for your own case. ``preprocessing.py`` loads ``config/<case>.yml`` and clips the configured DEM(s) to the ROI using the data directories defined in the YAML; ``run2postprocessing.py`` then builds the TELEMAC case from the same configuration and points to the HydroBayesCal calibration.

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
* optionally **breaklines** and **region/MATID points** to control the mesh and friction zones;
* optionally **hydraulic measurements** (water depth, flow velocity) that become the calibration data;
* optionally a **DEM-of-Difference** (initial − target) as topographic-change calibration data.

All inputs are declared by path in the configuration; relative paths are resolved against the configuration file's own directory, and any input in a different coordinate system is reprojected to the project CRS on ingest.

The pipeline
------------

``hydromate`` runs five stages (see :doc:`codedocs`):

#. **DEM → ROI** (:mod:`hydromate.dem`) — reproject and clip the DEM(s) to the boundary.
#. **Mesh + bathymetry** (:mod:`hydromate.mesh`, :mod:`hydromate.selafin`) — a gmsh triangular mesh from the boundary and breaklines with per-region size refinement, the DEM interpolated onto the nodes, written as a TELEMAC geometry ``.slf`` with friction zones embedded as a per-node ``FRIC_ID`` variable.
#. **Boundary conditions** (:mod:`hydromate.boundary`) — classify the mesh contour against the liquid-boundary lines and write the ``.cli``.
#. **Steering + friction** (:mod:`hydromate.steering`) — the TELEMAC-2D ``.cas`` and the zonal friction ``.tbl`` (and a GAIA ``.cas`` when morphodynamics is enabled).
#. **Calibration** (:mod:`hydromate.calibration`) — the calibration-points CSV and a ready HydroBayesCal ``config_Telemac.py``.

Configuration reference
-----------------------

A configuration file has these top-level sections:

``project``
    case name, ``crs_epsg`` (project coordinate system), and the output directories (``work_dir``, ``model_dir``, ``results_dir``).
``telemac``
    ``pysource`` (the TELEMAC environment script), ``solver`` and ``n_processors``.
``inputs``
    all input data paths (DEM(s), boundary, breaklines, region points, liquid boundaries, inflow, optional stage-discharge and measurements).
``mesh``
    background and breakline target edge lengths and per-MATID ``region_sizes``.
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
