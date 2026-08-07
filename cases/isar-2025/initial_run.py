"""Test-run the built TELEMAC case and check hotstart convergence (workflow step 1, continued).

After ``preprocessing.py`` has assembled the case in ``hydromate-case/simulation/``,
this launches the solver once on exactly that ``steady2d.cas`` to confirm the case
runs without crashing -- it does NOT rebuild anything. This concludes the
preprocessing step. Next run ``mesh_convergence_study.py``, then
hand off to HydroBayesCal.

Because this steady result is also the **hotstart** seed for the HydroBayesCal
calibration, the run then gets a flux / mass-balance convergence analysis
(``hydromate.flux_convergence``, reading the solver listing with
``hydromate.sortie``). It writes four files into the simulation folder -- ``extracted-fluxes.csv`` + ``flux-convergence.png`` (per-boundary
fluxes) and ``convergence-rate.csv`` + ``convergence-rate.png`` (the relative flux
imbalance and its convergence rate) -- and reports the simulation time at which the
boundary fluxes reach mass balance to the configured tolerance
(``hydrodynamics.flux_tolerance``, 1e-3 = 0.1% imbalance -- the grade that matters
when the result is read as discharge, water depth or velocity; 1e-4 is the stricter
grade for seeding a HydroBayesCal hotstart fleet).
When the fluxes stay balanced within 1e-3 m3/s over 10 consecutive listing printouts
(or, on a noisy steady state, in the 10-printout mean), a ``hotstart2d.cas`` is
written next to the steady case: it continues from the steady result ``r2d.slf``
with that steady time as ``DURATION`` and the constant Q / H prescriptions kept
alive. The per-processor ``*_p0000N.sortie`` copies of a parallel run are deleted.
See ``hydromate.flux_convergence``.

Needs ``telemac.pysource`` in case-config.yml to point at a real TELEMAC env. The
convergence analysis is built in (it used to require the external ``pythomac``
package).

Run: mamba run -n hydromate-env python cases/<your-case>/initial_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from hydromate import (
    format_flux_convergence,
    outlet_profile,
    run_solver_streaming,
    setup_logging,
    wetting_report,
    write_line_discharges,
)
from hydromate.config import load_config
from hydromate.env import TelemacRuntime
from hydromate.flux_convergence import analyze_flux_convergence

# optional CLI arg selects the scenario config, e.g.
#   python initial_run.py case-config-greenampt.yml
CONFIG = Path(__file__).resolve().parent / (
    sys.argv[1] if len(sys.argv) > 1 else "case-config.yml")
cfg = load_config(CONFIG)

# Number of parallel MPI processes for this test run. None -> use the core
# count assigned in preprocessing (case-config.yml telemac.n_processors);
# set an integer here to override it for this run only (e.g. NCSIZE = 8).
NCSIZE: int | None = None


def _water_table_depth(cfg):
    """Depth the bar's water table holds in place, per node (None when unused).

    Rebuilt here from the same config the build used, so the report and the model
    agree on which water is groundwater-fed rather than stray.
    """
    if cfg.percolation.water_table != "phreatic":
        return None
    try:
        import numpy as np

        from hydromate import selafin, watertable
        from hydromate.mesh import Mesh

        geo = selafin.read_slf(cfg.model_path(cfg.geometry_slf))
        mesh = Mesh(x=geo["x"], y=geo["y"], triangles=geo["ikle"],
                    bottom=np.asarray(geo["values"]["BOTTOM"], float),
                    ipobo=geo["ipobo"],
                    boundary_nodes=np.flatnonzero(np.asarray(geo["ipobo"]) > 0),
                    element_matid=np.ones(len(geo["ikle"]), int),
                    node_matid=np.ones(len(geo["x"]), int))
        ic = selafin.read_slf(cfg.model_path(cfg.ic_slf))["values"]["WATER DEPTH"]
        plane = watertable.fit_phreatic_plane(
            cfg, mesh, surface=np.asarray(mesh.bottom, float) + ic)
        if plane is None:
            return None
        return watertable.water_table_depth(
            plane, mesh, watertable.patch_node_mask(cfg, mesh))
    except Exception as exc:  # noqa: BLE001 - a reporting aid, never fatal
        print(f"water-table mask unavailable: {type(exc).__name__}: {exc}")
        return None


def main() -> None:
    cas = cfg.model_path(cfg.cas_file)
    if not cas.exists():
        print(f"no built case at {cas}.")
        print("run  python cases/<your-case>/preprocessing.py  first.")
        return

    setup_logging(cfg.model_path(cfg.log_file))   # append to the simulation log
    print(f"test-running the built case: {cas}")
    print("streaming TELEMAC output (simulated-time progress bar below):\n")

    runtime = TelemacRuntime(cfg.telemac)
    try:
        runtime.check_available()
        # stream the solver listing live and show a simulated-time vs DURATION
        # progress bar instead of running silently (see hydromate.progress).
        proc = run_solver_streaming(runtime, cfg, ncsize=NCSIZE)
    except Exception as exc:  # noqa: BLE001 - report cleanly
        print(f"could not run the solver: {type(exc).__name__}: {exc}")
        return

    if proc.returncode != 0:
        print(f"FAILED - solver returned {proc.returncode}; "
              f"see {cfg.model_path(cfg.log_file)}")
        return

    print(f"OK - the case runs. Results: {cfg.model_path(cfg.results_slf)}")

    # convergence: when did the boundary fluxes reach mass balance? The tolerance
    # comes from hydrodynamics.flux_tolerance (1e-3, the grade that matters when the
    # result is read as discharge / depth / velocity); tighten it to
    # HOTSTART_TOLERANCE (1e-4) when this result is to seed a calibration fleet.
    try:
        fc = analyze_flux_convergence(cfg)
    except Exception as exc:  # noqa: BLE001 - the run already succeeded; report cleanly
        print(f"convergence analysis skipped: {type(exc).__name__}: {exc}")
    else:
        for line in format_flux_convergence(fc):
            print(line)

    # discharge across the internal cross-sections ("baffles"): how the total Q
    # splits between the threads of the braided reach. Written next to the results
    # as baffle-XS-q.csv (Baffle Name, discharge in m3/s).
    if cfg.geodata.control_sections is not None:
        try:
            df = write_line_discharges(
                cfg.model_path(cfg.results_slf),
                cfg.geodata.control_sections,
                cfg.model_path("baffle-XS-q.csv"),
                geometry=cfg.model_path(cfg.geometry_slf),
                name_field=cfg.geodata.control_section_name_field,
                crs_epsg=cfg.crs_epsg,
            )
        except Exception as exc:  # noqa: BLE001 - the run already succeeded
            print(f"cross-section discharges skipped: {type(exc).__name__}: {exc}")
        else:
            print(f"\ncross-section discharges -> {cfg.model_path('baffle-XS-q.csv')}")
            for _, r in df.iterrows():
                print(f"  {r['name']:<16} {r['discharge']:8.4f} m3/s"
                      f"   (wet {r['wetted_width']:5.1f} m, mean h {r['mean_depth']:.3f} m,"
                      f" mean |U| {r['mean_velocity']:.3f} m/s)")

    # WHERE the water sits. A balanced flux budget says nothing about wetted extent:
    # water seeded above the level the run converges to cannot leave a 2D model (no
    # infiltration, no evaporation) and survives as stagnant film. The report splits
    # the wetted area into active flow / film / isolated puddles, attributes the film
    # to the pre-wet seed, and shows whether it is still draining. The outlet profile
    # then checks the prescribed outflow stage against the reach's own surface slope.
    try:
        rep = wetting_report(
            cfg.model_path(cfg.results_slf),
            geometry=cfg.model_path(cfg.geometry_slf),
            initial_conditions=cfg.model_path(cfg.ic_slf),
            # water the bar's water table holds in place is legitimately wet: without
            # this it would be counted as film AND as an isolated puddle, i.e.
            # reported as a defect when it is exactly what the model intends
            supported=_water_table_depth(cfg),
            wet_depth=cfg.hydrodynamics.wet_depth,
            out=cfg.model_dir,
        )
    except Exception as exc:  # noqa: BLE001 - the run already succeeded
        print(f"wetting report skipped: {type(exc).__name__}: {exc}")
    else:
        print(f"\nwetted-extent report -> {cfg.model_path('wetting-report.csv')}")
        for line in rep.summary():
            print(f"  {line}")

    try:
        prof = outlet_profile(cfg, cfg.model_path(cfg.results_slf),
                              geometry=cfg.model_path(cfg.geometry_slf),
                              out=cfg.model_dir)
    except Exception as exc:  # noqa: BLE001 - the run already succeeded
        print(f"outlet profile skipped: {type(exc).__name__}: {exc}")
    else:
        print(f"\noutlet profile -> {cfg.model_path('outlet-profile.csv')}")
        for line in prof.summary():
            print(f"  {line}")

    print("next: python cases/<your-case>/mesh_convergence_study.py")


if __name__ == "__main__":
    main()
