Calibration & validation (HydroBayesCal)
========================================

Once :doc:`the pipeline <usage>` has produced a calibration-ready case and
``initial_run.py`` has confirmed it runs and is converged, the model is calibrated
with `HydroBayesCal <https://github.com/Ecohydraulics/hydrobayescal>`_ (HBC) -
surrogate-assisted Bayesian calibration with quantified uncertainty.

What hydromate hands over
-------------------------

Pipeline stage 5 writes two artifacts into the calibration directory
(``hydromate-case/calibration-validation/``):

* ``measurements-calibration.csv`` - the calibration points, one row per
  measurement location: ``id, x, y, z, <QTY>_DATA, <QTY>_ERROR`` for each
  configured ``calibration_quantity`` (``<QTY>`` is a SELAFIN variable name such as
  ``WATER DEPTH`` or ``SCALAR VELOCITY``), compiled from the
  :ref:`ground truth <input-ground-truth>`;
* ``config_Telemac.py`` - the ready HydroBayesCal configuration: it references the
  built ``steady2d.cas``, the calibration CSV, the calibration **parameters** and
  their ranges, and the sampling settings - all taken from the ``calibration``
  block of the YAML.

The **hotstart**. The steady result confirmed by ``initial_run.py`` is also the
hotstart seed for the calibration: every perturbed HBC run continues from it, so it
must already be at a converged, mass-conservative state (that is exactly what the
flux-convergence check in ``initial_run.py`` verifies, at a tight 1e-6 tolerance).

The ``calibration`` config block
---------------------------------

.. code-block:: yaml

   calibration:
     calibration_quantities: ["WATER DEPTH"]                    # measured quantities calibrated against
     extraction_quantities: ["WATER DEPTH", "SCALAR VELOCITY"]  # quantities pulled from the results
     measurement_error: 0.10      # fallback error, fraction of the measured value
     init_runs: 30                # initial (space-filling) design samples
     max_runs: 50                 # total runs (initial + active-learning iterations)
     gp_library: gpy
     parameters:
       - { name: zone1, min: 0.05, max: 0.50, comment: "channel ks [m]" }
       - { name: zone2, min: 0.10, max: 1.00, comment: "floodplain ks [m]" }

The parameter ``name`` follows HydroBayesCal's prefix convention (see
:ref:`Calibration parameter naming <input-config>`): ``zone<N>`` perturbs the
Nikuradse :math:`k_s` of friction zone ``N`` in the ``.tbl``;
``gaiaCLASSES SHIELDS PARAMETERS <n>`` a GAIA sediment parameter; any literal
TELEMAC keyword is written straight into the steering file.

Running the calibration
-----------------------

.. code-block:: bash

   cd cases/<your-case>/hydromate-case/calibration-validation
   python /path/to/hydrobayescal/bal_telemac.py --config config_Telemac.py

HydroBayesCal then builds a Gaussian-process surrogate over the parameter space,
refines it by active learning (Bayesian active learning), and returns posterior
distributions for the calibration parameters together with the calibrated model
response. The **validation** quantities (e.g. ``SCALAR VELOCITY`` when the
calibration is on ``WATER DEPTH``) are extracted at the same points so the
calibrated model can be checked against measurements it was not fitted to.

.. note::

   v1 covers the **2D hydraulic** calibration (water depth, velocity). The GAIA
   morphodynamic / DEM-of-Difference (topographic-change) calibration is wired as
   an extension point - enable ``morphodynamics`` and declare sediment classes, and
   add ``gaiaCLASSES ...`` calibration parameters.
