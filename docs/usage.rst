Usage
=====

``hydromate`` is driven by a single YAML configuration file and exposes one command. The workflow is: write a config, build the case, then calibrate it with HydroBayesCal.

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
