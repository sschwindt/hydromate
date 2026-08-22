aXqua
=====

**Automated, reproducible setup of calibration-ready river models - TELEMAC and OpenFOAM - driven from QGIS, with a persistent job runner behind it.**

``axqua`` turns raw field and geospatial data into a production-ready `TELEMAC <http://www.opentelemac.org/>`_ 2D/3D model (with GAIA morphodynamics), or an `OpenFOAM <https://openfoam.org/>`_ ``interFoam`` free-surface case, wired directly into `HydroBayesCal <https://github.com/Ecohydraulics/hydrobayescal>`_ for surrogate-assisted Bayesian calibration. The result is a numerical model that can be run with **quantified uncertainty** on physically meaningful calibration parameters, such as bed-friction zones and sediment-transport parameters.

The **QGIS plugin** is the normal way to use it: your geodata is already open there, so a case is chosen, built, submitted, watched and loaded back as styled layers from the same map canvas. Simulations run as **persistent jobs** outside QGIS, so they keep going after the window that started them is gone. Everything remains fully usable from the command line with QGIS absent.

About
-----

Building a river model by hand is slow, error-prone and hard to reproduce: a mesh has to be generated, a digital elevation model interpolated onto it, boundary conditions encoded node-by-node, steering and friction files written, and measurements reshaped into a calibration table - each step a chance for a silent inconsistency, such as a mismatched coordinate system, a boundary numbering that disagrees with the mesh, or a friction zone that no longer maps to the terrain.

``axqua`` automates that whole chain from a single, validated configuration file, so that the same inputs always produce the same model, every artifact agrees with every other - the mesh boundary order *is* the boundary-condition order, and the friction zones *are* the calibration parameters - and the modeller's effort moves from clicking through GIS dialogs to deciding the physics.

Given an initial state, and optionally a target state for morphodynamics, it produces a calibration-ready case plus the HydroBayesCal configuration needed to calibrate it. The **initial state** drives the hydraulic calibration (water depth, velocity); an optional **target state** - a second DEM, or a DEM-of-Difference - drives an optional morphodynamic calibration of sediment transport and topographic change.

The workflow
~~~~~~~~~~~~

One case description feeds **two simulation backends**. The geodata, the boundary conditions, the roughness, the structures and the ground truth are shared; each solver adds only the knobs that are genuinely its own. The whole thing runs in this order:

**1. Common preprocessing** (:doc:`preprocessing`). Describe the reach once: the region of interest, the liquid boundaries, the mesh zones and channel centerline, the roughness zones, any structures, and the field measurements that will become calibration targets. Then build the case - the DEM is clipped, an anisotropic mesh is generated with cells elongated along the flow, the bathymetry is interpolated onto it, the boundary nodes are classified against your inflow and outflow lines, and the solver input files are written. Nothing is launched yet, and everything the geometry decides is logged so it can be checked before any compute is spent.

**2. Initial and boundary conditions.** The discharge, the downstream stage or rating curve, and how the model starts: a dry bed with a thin plug at the inflow so the prescribed-discharge boundary can establish, or a pre-wetted channel seeded at the stage that actually conveys the case discharge through real cross-sections. Both are set in the config and reported before the run (:doc:`telemac`).

**3. Simulation options.** Finite elements or finite volumes, the turbulence model (auto-selected from what the mesh resolves), steady or hydrograph-driven, 2D or a 3D extension hotstarted from the 2D result, GAIA morphodynamics, or a gain-lose reach that exchanges flow with a porous body. The OpenFOAM path (:doc:`openfoam`) adds a resolved two-phase free surface where the vertical structure of the flow matters, and seeds itself from a TELEMAC run so it does not have to start cold.

**4. Mesh convergence.** Once the initial run confirms the model runs and its boundary fluxes balance, the grid-independence study rebuilds and re-runs the case over a ladder of mesh resolutions and compares the results, so the answer you report is the physics rather than the discretisation. The 3D path gets its own study over the number of vertical layers, since that is a discretisation choice the 2D study never covered.

**5. Bayesian calibration** (:doc:`hbc`). The built case, the calibration-point CSV and the parameter ranges go to HydroBayesCal, which trains a Gaussian-process surrogate over the parameter space, refines it by Bayesian active learning, and returns posterior distributions for the calibration parameters - friction zones, Shields parameters, whatever you declared - together with the calibrated model response and its uncertainty. Validation quantities you did not calibrate against are extracted at the same points, so the result can be checked against measurements it never saw.

**6. Postprocessing, visualization and export** (:doc:`results`). What the run produced, where it landed, and whether it can be believed: the flux-convergence check, the wetted-extent report that separates flowing water from stagnant film, the outlet profile, and the discharge across your own cross-sections. Then the results as styled QGIS layers, as ParaView fields, as CSV tables, as a print layout or as an animation.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   installation
   qgis_plugin
   preprocessing
   telemac
   openfoam
   hbc
   results
   advanced
   help
   development
   license
