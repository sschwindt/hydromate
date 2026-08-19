Outputs
=======

Everything aXqua produces lands under one folder per case, ``axqua-case/``, split by workflow phase. Raw inputs stay immutable in ``user-sources/``, so a case can always be rebuilt from scratch, and both folders are gitignored: they are local state, often tens of gigabytes of it.

The case folder
---------------

.. code-block:: text

   cases/<name>/
     case-config.yml            # the case description (tracked)
     user-sources/              # your raw inputs: DEMs, geodata, ground truth (never written to)
     axqua-case/
       preprocessing/           # DEM clips, the DEM-of-Difference, the compiled ground-truth table
       simulation/              # the built TELEMAC case and its results
       openfoam/                # the OpenFOAM case (0/ constant/ system/ processor*/)
       postprocessing/          # mesh-convergence/ and vertical-convergence/ studies
       calibration-validation/  # what HydroBayesCal is handed

The per-phase directories are configurable in the ``project`` block (``preprocessing_dir``, ``model_dir``, ``postprocessing_dir``, ``calibration_dir``); the OpenFOAM case sits beside ``simulation/`` rather than inside it, because an OpenFOAM case owns its whole directory.

Each phase writes its own timestamped ``axqua.log`` into its own output folder, at DEBUG level and in append mode, mirroring what the console shows. Every pipeline stage and every heavy sub-step is bracketed by ``START`` / ``DONE … in N.NNs`` lines, so a log is also a per-step timing record. Pass ``-v`` for DEBUG on the console too.

What the build writes
---------------------

From a region-of-interest DEM, inflow data, an optional stage-discharge relation and hydraulic measurements, the build produces a complete, calibration-ready case:

#. a clipped DEM for the region of interest, in ``preprocessing/`` (plus the DEM-of-Difference when a target DEM is configured);
#. a triangular mesh with interpolated bathymetry, written as a TELEMAC geometry ``geometry.slf``, with friction zones embedded as a per-node ``FRIC_ID`` variable;
#. a boundary-conditions ``boundaries.cli``;
#. a TELEMAC-2D steering ``steady2d.cas`` and a zonal friction ``friction.tbl`` (plus ``unsteady2d.cas`` and a ``.liq`` liquid-boundaries file when the inflow varies, and a GAIA ``.cas`` when morphodynamics is enabled);
#. the initial-conditions SELAFIN the run continues from;
#. a calibration-points CSV and a ready-to-run HydroBayesCal ``config_Telemac.py``, in ``calibration-validation/``.

Reports from a run
------------------

A solver writes results; aXqua writes the reports that say whether those results can be believed. All of them land next to the case they describe, in ``simulation/``.

**Flux convergence.** After the initial run, :func:`axqua.analyze_flux_convergence` reads the ``.sortie`` listing and writes ``extracted-fluxes.csv`` and ``flux-convergence.png`` (the per-boundary fluxes) plus ``convergence-rate.csv`` and ``convergence-rate.png`` (the relative imbalance and its rate). The per-processor ``*_p0000N.sortie`` copies of a parallel run are deleted, since only the merged main listing matters. When the **absolute** flux imbalance ``||Q_in| - |Q_out||`` stays below 1e-3 m³/s over 10 consecutive listing printouts - or, on a noisy steady state, in the 10-printout mean - a ``hotstart2d.cas`` is generated next to the steady case: it continues from ``r2d.slf`` with that steady time as ``DURATION`` and the constant Q/H prescriptions unchanged (:func:`axqua.steering.write_hotstart_cas`). That hotstart is what the calibration runs continue from, so this check is also the gate on :doc:`hbc`.

**Where the water is** (:func:`axqua.report_wetting`). A balanced flux budget says nothing about wetted *extent*, and the two are independent failure modes: a run can close its budget to 1e-4 and still show water standing where the reach has none. ``wetting-report.csv`` splits the wetted area into **active** flow, stagnant **film** and **isolated puddles**, says how much of each the initial condition put there, and - by re-reading earlier frames - whether the film is still draining or has **plateaued**. The distinction matters because a 2D model has neither infiltration nor evaporation: water seeded above the level the run converges to can never leave, so a plateaued film is a defect no amount of extra runtime will fix. Water an external source holds in place (a :ref:`water-table pool <usage-gain-lose>`) is reported separately, so it does not read as a defect.

**The outflow boundary** (``outlet-profile.csv``). The flowing nodes are banded by distance from the outflow and the near-boundary surface slope is compared with the reach's own, returning ``backwater`` (the prescribed stage is holding water up over ground that should be dry), ``drawdown`` or ``neutral``.

.. note::

   The wetted threshold in these reports is ``hydrodynamics.wet_depth`` (0.01 m). This is a *reporting* convention rather than a model setting: on a bed with Nikuradse ``ks`` 0.05-0.5 m, water 5 mm deep stands *within* the grain roughness rather than flowing over it. Filter the result in ParaView at the same depth so the picture and the report agree. To remove such water from the model instead, see the ``drying`` block.

**Discharge across your own cross-sections** (:func:`axqua.report_sections`). With ``geodata.control_sections`` set, each line of that layer is integrated from the result (``Q = int (H*U).n ds``) into ``baffle-XS-q.csv``. Because it reads the *result* rather than the steering file, sections can be drawn and moved in GIS without re-running the solver - which is how the split of the total discharge between the threads of a braided reach is read off and checked against field transects.

**Surface freedom** (OpenFOAM). After an ``interFoam`` run, the report states the inlet and outlet water discharge and their relative imbalance, judged against the same ``hydrodynamics.flux_tolerance`` the 2D run is judged by, and whether the water ever reached the lid or the lateral wall. The latter is the question nothing else in the output would reveal: if it did, the answer was set by a meshing decision rather than by the hydraulics, and ``freeboard`` or ``wet_margin`` has to grow (see :doc:`openfoam`).

**Convergence studies.** The mesh-convergence study writes its per-level builds, runs and comparison into ``postprocessing/mesh-convergence/``, and the 3D vertical-layer study into ``postprocessing/vertical-convergence/``, each with its own ``axqua.log``.

.. note::

   The anisotropic mesh is **not reproducible byte-for-byte** between builds: BAMG fails intermittently and the retry ladder then meshes with a coarser background metric, giving a different but equally valid mesh (~0.4% spread in node count on the isar-2025 reach). So ``geometry.slf``, ``boundaries.cli`` and ``initial-conditions.slf`` cannot be byte-compared between runs, a resumed mesh-convergence study may compare levels built at different retry rungs, and a published result should quote the ``geometry.slf`` actually used rather than assume it can be regenerated exactly. The OpenFOAM mesh is aXqua's own structured lattice and *is* reproducible.

What the calibration is handed
------------------------------

Stage 5 of the build writes two artifacts into ``calibration-validation/``:

* ``measurements-calibration.csv`` - the calibration points, one row per measurement location: ``id, x, y, z, <QTY>_DATA, <QTY>_ERROR`` for each configured calibration quantity, compiled from the :ref:`ground truth <input-ground-truth>`;
* ``config_Telemac.py`` - the ready HydroBayesCal configuration, referencing the built case, the calibration CSV, the calibration parameters and their ranges, and the sampling settings.

:doc:`hbc` describes what happens with them.
