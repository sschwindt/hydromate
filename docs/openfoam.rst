OpenFOAM workflow
=================

Use OpenFOAM when the **vertical structure** of the flow matters *and* the water surface has a gradient - which rules out ``simpleFoam`` and means a two-phase VOF solver, ``interFoam``. It reads the same case description as the TELEMAC path (:doc:`preprocessing`), so nothing has to be drawn or configured twice.

**TELEMAC runs first, automatically.** OpenFOAM is the expensive model in the chain, and almost everything it needs to know at ``t = 0`` is something a 2D depth-averaged run answers in minutes: where the water is, how deep it is, how fast it moves, and roughly where its surface sits. So the build seeds itself - it reuses the case's own converged ``r2d.slf`` when there is one, and otherwise builds and runs a **coarse TELEMAC pre-run** of its own (54 s on isar-2025 at ``size_scale: 4``, producing a 3D mesh within 3% of the one the full production result gives). That seed does four jobs at once: the wetted cells start wet, the lid is clamped just above the surface so most air cells never exist, the plan footprint is trimmed to the wetted corridor, and every column starts with a velocity.

Switch it off with ``openfoam.pre_run.enabled: false``, ``--no-pre-run``, or the ``PRE_RUN = False`` toggle at the top of ``openfoam_preprocessing.py``. The case is then built **cold** under a flat lid, which is what happens without a seed: many times more air cells, plus the whole filling transient, in the slowest solver you have.

Build and run
-------------

.. code-block:: bash

   axqua openfoam cases/example-Inn/case-config.yml --check   # cell count, no build
   python cases/example-Inn/openfoam_preprocessing.py             # build the case
   python cases/example-Inn/openfoam_run.py                       # spin-up, run, report

#. **Build** - a terrain-following, all-hexahedral mesh written straight to ``constant/polyMesh`` (no snappyHexMesh: a river bed is a height field, so it is *followed* rather than snapped to), fields seeded from the converged ``r2d.slf``, and the two staged dictionary sets. The build prints its own **cost report** - the time step the Courant target will settle on, the number of steps your ``end_time`` implies, and how that compares with one flush of the reach.
#. **Run** - ``checkMesh``, ``decomposePar``, then two stages of ``interFoam``: a short spin-up that settles the interface from the depth-averaged hotstart, then the production stage. Watch ``Co`` and ``dt`` on the progress bar; a healthy run holds ``dt`` near the Courant target.
#. **Report** - inlet and outlet **water** discharge and their relative imbalance, judged against the same ``hydrodynamics.flux_tolerance`` the 2D run is judged by, plus the **surface-freedom** verdict described in :doc:`results`.

The air phase is the usual reason such runs fail, so three things address it: the **lid follows the 2D free surface** at a fixed ``freeboard`` so most air cells never exist; **semi-implicit MULES** lets the Courant target run near 0.9 instead of the tutorials' 0.2; and a **``limitVelocity`` constraint** caps ``|U|`` at several times the reach's own water speed - water never reaches it, a runaway air jet does.

The seed is an approximation, and is treated as one
---------------------------------------------------

TELEMAC is a model too. The 3D free surface is still solved and free to move away from the 2D answer - that is what ``freeboard`` (how far it may rise) and ``wet_margin`` (how far it may spread) are room for. Under ``headroom_mode: auto`` both are sized from the seed's own flow rather than guessed: the velocity head ``V^2/2g`` the water could convert at a stagnation point or the outside of a bend, plus a quarter of the depth for what a depth-averaged model cannot see at all, with the configured values as **floors**. And after the run, ``surface_freedom`` reports whether the water ever reached the lid or the lateral wall - i.e. whether the answer was set by a meshing decision rather than by the hydraulics, which nothing else in the output would reveal.

For the vertical structure of the seed itself, ``pre_run.dimension: 3d`` follows the 2D pre-run with a hydrostatic TELEMAC-3D run and seeds each OpenFOAM cell at its own elevation, instead of giving every column one depth-averaged velocity from bed to surface. That is the one case where prescribing vertical structure beats starting flat: it was computed by a solver on this reach's own bathymetry, not assumed from a log law whose normalisation is inadmissible on a gravel bed anyway.

**It is not available on every reach, and aXqua checks before it spends the run.** TELEMAC-3D has only sigma planes, and their count is sized from the flow depth against the horizontal cell size, refusing cells more than four times taller than wide. A reach that is shallow relative to its plan mesh therefore has no room for an interior level at all: isar-2025 is 0.26 m deep with 0.62 m cells and gets exactly **two** planes - one layer, which is a depth-averaged answer at 3D cost. Asking for ``dimension: 3d`` there returns in seconds with that reason and keeps the 2D seed. To make it available, the reach has to be deeper, or the plan mesh coarser.

Where it does run, the seed run is deliberately made **robust rather than faithful**: it cold-starts at a constant depth rather than continuing the 2D surface (which, on a braided bed, lifts a great many dry columns onto the sigma mesh and diverges on the first solve), and it uses k-epsilon rather than Spalart-Allmaras (which diverges in the vertical diffusion of velocity on a wetting/drying bed). A seed needs a plausible profile, not agreement with the 2D surface. The result is checked for NaN before it is adopted.

.. _usage-rigid-lid:

Getting there cheaply: the rigid lid
------------------------------------

Even tamed, the two-phase run is expensive, and most of what goes wrong first - a mesh ``checkMesh`` rejects, an inlet that is not where you thought, a roughness that is too coarse for the depth - has nothing to do with the air. ``openfoam.mode: rigid-lid`` removes the air phase **by construction** rather than damping it: the lid is placed *on* the 2D free surface as a ``slip`` wall, so the domain holds water only and ``alpha`` is identically 1. ``interFoam`` then degenerates to a single-phase run while ``p_rgh`` and gravity stay exactly as they are, which is what keeps the result a recognisable river rather than a duct flow.

Three consequences follow, and together they are the speed-up:

* there is **no interface**, so no interface Courant limit and no spin-up stage;
* ``alpha`` has nothing to advect, so the implicit MULES correction is dropped - measured at four fifths of the run time on its own;
* the cells that were air - about nine tenths of them - are simply absent.

Combine it with ``cell_size_factor``, which coarsens relative to the 2D channel resolution so a test run cannot silently drift away from the mesh being tested:

.. list-table::
   :header-rows: 1
   :widths: 20 12 20 16 32

   * - mode
     - plan dx
     - cells
     - time step
     - 300 s of river (isar-2025, 1 core)
   * - ``vof``
     - 0.5 m
     - 1,579,074
     - 3.1e-3 s
     - ~97,600 steps
   * - ``rigid-lid``, factor 3
     - 1.5 m
     - 36,164
     - 1.8e-1 s
     - ~1,700 steps, ~8 min (60 s measured in 96 s)
   * - ``rigid-lid``, factor 5
     - 2.5 m
     - 14,632
     - 3.7e-1 s
     - 832 steps, **91 s wall-clock**

**What you give up is the free surface.** It can no longer move, so the mode cannot tell you about a hydraulic jump, a standing wave, or superelevation through a bend - exactly the questions a 3D free-surface model is usually bought for. It is a way to reach a trustworthy ``vof`` run quickly, not a substitute for one.

Two knobs exist only in this mode. ``min_water_depth`` (0.20 m) leaves shallower columns out of the mesh; it cannot be raised freely, because a reach whose inflow section is itself shallow will lose its inlet patch to the trim. The layer count is then **fitted to that depth** - cells thinner than the bed grains are meaningless, and thin enough that the bed's variation within one plan cell folds them - so 14 layers over 0.20 m is reduced to 4, and the build says so. ``auto_bed_layer`` defaults *off* here for the same reason: pinning a thick bed layer into a shallow column is what turns the layers above it into folded slivers.
