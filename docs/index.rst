hydromate
=========

**Automated, reproducible setup of calibration-ready TELEMAC hydro- and morphodynamic models.**

``hydromate`` turns raw field and geospatial data into a production-ready `TELEMAC <http://www.opentelemac.org/>`_ 2D (and, as an extension point, GAIA morphodynamic) model, wired directly into `HydroBayesCal <https://github.com/Ecohydraulics/hydrobayescal>`_ for surrogate-assisted Bayesian calibration. The result is a numerical model that can be run with **quantified uncertainty** on physically meaningful calibration parameters (bed friction zones, sediment-transport parameters).

Motivation
----------

Building a TELEMAC model by hand is slow, error-prone and hard to reproduce: a mesh has to be generated, a digital elevation model interpolated onto it, boundary conditions encoded node-by-node, steering and friction files written, and measurements reshaped into a calibration table — each step a chance for a silent inconsistency (a mismatched coordinate system, a boundary numbering that disagrees with the mesh, a friction zone that no longer maps to the terrain).

``hydromate`` automates that whole chain from a single, validated configuration file, so that:

* the same inputs always produce the same model (reproducibility);
* every artifact agrees with every other (the mesh boundary order *is* the boundary-condition order, friction zones *are* the calibration parameters);
* the modeller's effort moves from clicking through GIS dialogs to deciding the physics — mesh resolution, friction laws, calibration ranges.

Goal
----

Given an initial state (and, optionally, a target state for morphodynamics), produce a calibration-ready TELEMAC case plus the HydroBayesCal configuration needed to calibrate it:

* the **initial state** drives the hydraulic calibration (water depth, velocity);
* an optional **target state** (a second DEM / DEM-of-Difference) drives an optional morphodynamic (sediment-transport / topographic-change) calibration.

What it produces
----------------

From a region-of-interest DEM, inflow data, an optional stage-discharge relation and hydraulic measurements, ``hydromate`` runs a five-stage pipeline and writes:

#. a clipped DEM for the region of interest;
#. a triangular mesh with interpolated bathymetry, as a TELEMAC geometry ``.slf`` (friction zones embedded as a per-node ``FRIC_ID`` variable);
#. a boundary-conditions ``.cli`` file;
#. a TELEMAC-2D steering ``.cas`` file and a zonal friction ``.tbl``;
#. a calibration-points CSV and a ready-to-run HydroBayesCal ``config_Telemac.py``.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   installation
   usage
   codedocs
   license
