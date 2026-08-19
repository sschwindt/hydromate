aXqua
=====

**Automated, reproducible setup of calibration-ready river models - TELEMAC and OpenFOAM - with a persistent job runner and a QGIS frontend.**

``axqua`` turns raw field and geospatial data into a production-ready `TELEMAC <http://www.opentelemac.org/>`_ 2D/3D model (with GAIA morphodynamics), or an `OpenFOAM <https://openfoam.org/>`_ ``interFoam`` free-surface case, wired directly into `HydroBayesCal <https://github.com/Ecohydraulics/hydrobayescal>`_ for surrogate-assisted Bayesian calibration. The result is a numerical model that can be run with **quantified uncertainty** on physically meaningful calibration parameters, such as bed-friction zones and sediment-transport parameters.

Simulations run as **persistent jobs** that keep going after the shell - or QGIS - that started them is gone, and can be rediscovered, monitored and cancelled later. There is a **QGIS plugin** frontend for the whole workflow, and everything remains fully usable from the command line with QGIS absent.

About
-----

Building a river model by hand is slow, error-prone and hard to reproduce: a mesh has to be generated, a digital elevation model interpolated onto it, boundary conditions encoded node-by-node, steering and friction files written, and measurements reshaped into a calibration table - each step a chance for a silent inconsistency, such as a mismatched coordinate system, a boundary numbering that disagrees with the mesh, or a friction zone that no longer maps to the terrain.

``axqua`` automates that whole chain from a single, validated configuration file, so that:

* the same inputs always produce the same model (reproducibility);
* every artifact agrees with every other - the mesh boundary order *is* the boundary-condition order, and the friction zones *are* the calibration parameters;
* the modeller's effort moves from clicking through GIS dialogs to deciding the physics: mesh resolution, friction laws, calibration ranges.

Given an initial state, and optionally a target state for morphodynamics, it produces a calibration-ready case plus the HydroBayesCal configuration needed to calibrate it. The **initial state** drives the hydraulic calibration (water depth, velocity); an optional **target state** - a second DEM, or a DEM-of-Difference - drives an optional morphodynamic calibration of sediment transport and topographic change.

One case description feeds **two simulation backends**. The geodata, the boundary conditions, the roughness, the structures and the ground truth are shared; each solver adds only the knobs that are genuinely its own. Prepare the case once (:doc:`preparation`), run the depth-averaged model (:doc:`telemac`), and add a resolved free-surface model where the vertical structure of the flow matters (:doc:`openfoam`).

.. toctree::
   :maxdepth: 2
   :caption: Contents

   installation
   preparation
   telemac
   openfoam
   outputs
   hbc
   jobs
   qgis_plugin
   architecture
   codedocs
   help
   license
