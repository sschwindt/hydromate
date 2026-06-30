"""Channel pre-wetting (warm-start) tests.

Covers the two pieces that make up the pre-wetting option used by the
mesh-convergence study: the warm-start SELAFIN writer
(:func:`hydromate.selafin.write_initial_state`) and the warm-start keywords the
steering writer emits (:func:`hydromate.steering.write_cas`). The TELEMAC solver
is not involved.
"""

from __future__ import annotations

import numpy as np

from hydromate import selafin


def test_write_initial_state_roundtrips(tmp_path):
    # a 2-triangle unit square; channel = the two left nodes pre-wetted to 0.5 m
    x = np.array([0.0, 1.0, 0.0, 1.0])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    ikle = np.array([[1, 2, 3], [2, 4, 3]])
    ipobo = np.array([1, 2, 4, 3])
    depth = np.array([0.5, 0.0, 0.5, 0.0])

    p = selafin.write_initial_state(tmp_path / "ic.slf", x, y, ikle, ipobo, depth)
    data = selafin.read_slf(p)

    assert data["var_names"] == ["VELOCITY U", "VELOCITY V", "WATER DEPTH"]
    assert np.allclose(data["values"]["WATER DEPTH"], depth)
    assert np.allclose(data["values"]["VELOCITY U"], 0.0)
    assert np.allclose(data["values"]["VELOCITY V"], 0.0)
    # same mesh as the geometry it initialises
    assert np.allclose(data["x"], x) and np.allclose(data["y"], y)
    assert np.array_equal(data["ikle"], ikle - 1)


def test_longitudinal_prewet_smooths_surface():
    """The thalweg-referenced seed yields a smooth surface following the reach slope:
    no negative depths, ~prewet_depth over the thalweg, deeper there than at the
    banks, and a thalweg depth ~constant along the reach (a smooth surface following
    the gradient, not the jagged constant-depth seed)."""
    from hydromate.steering import _longitudinal_prewet_depth

    ns, nc = 60, 21
    s = np.linspace(0.0, 300.0, ns)
    cross = np.linspace(-1.0, 1.0, nc)              # cross-channel position
    S, _ = np.meshgrid(s, cross, indexing="ij")
    bottom = (0.02 * S + (cross ** 2)[None, :]).ravel()   # slope + V cross-section
    s_node = S.ravel()
    mask = np.ones(bottom.size, dtype=bool)
    mask[: nc] = False                             # first section = dry floodplain

    depth = _longitudinal_prewet_depth(s_node, bottom, mask, depth_val=1.0)

    assert (depth >= 0).all()
    assert np.all(depth[~mask] == 0.0)             # floodplain stays dry
    sec = depth.reshape(ns, nc)
    thalweg = sec[:, nc // 2]                       # deepest column (V thalweg)
    assert (thalweg[ns // 5: -ns // 5] > sec[ns // 5: -ns // 5, 0]).all()  # > banks
    assert abs(thalweg[ns // 5: -ns // 5].mean() - 1.0) < 0.3   # ~ prewet_depth
    assert thalweg[ns // 5: -ns // 5].std() < 0.05 * thalweg.mean()  # smooth


def test_prescribed_flowrates_split_across_inflows(tmp_path):
    """The total reach Q is distributed across multiple inflow boundaries by node
    count (not prescribed in full on each, which would multiply the supplied
    discharge and flood the domain)."""
    from hydromate.boundary import LiquidBoundary
    from hydromate.steering import _prescribed_arrays

    cfg = _minimal_cfg(tmp_path)
    # two inflows (75 + 25 nodes) and one outflow
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=75),
               LiquidBoundary(index=2, kind="outflow", n_nodes=80),
               LiquidBoundary(index=3, kind="inflow", n_nodes=25)]
    flow, elev, prof = _prescribed_arrays(cfg, liquids, inflow_q=47.0, outflow_wse=373.8)

    shares = [float(v) for v in flow.split(";")]
    assert shares == [35.25, 0.0, 11.75]          # 47 * 75/100, outflow 0, 47 * 25/100
    assert abs(sum(shares) - 47.0) < 1e-9         # total inflow == reach Q
    assert elev.split(";")[1] == "373.8000"       # outflow stage on the outflow slot


def test_steering_finite_element_default_vs_finite_volume(tmp_path):
    """Default steering is now finite elements (the FE numerics + lateral wall
    friction + the configured turbulence model); opting into finite_volumes=True
    emits the HLLC FV numerics and forces turbulence model 1 instead."""
    from hydromate.boundary import LiquidBoundary
    from hydromate.steering import write_cas

    cfg = _minimal_cfg(tmp_path)
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=10),
               LiquidBoundary(index=2, kind="outflow", n_nodes=10)]

    # default kernel: finite elements with the resolved turbulence model (here 3)
    fe = write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=373.8,
                   turbulence_model=3).read_text()
    assert "SAINT-VENANT FV" not in fe
    assert "SCHEME FOR ADVECTION OF VELOCITIES : 14" in fe
    assert "TIDAL FLATS : YES" in fe and "SOLVER : 2" in fe
    assert "LAW OF FRICTION ON LATERAL BOUNDARIES" in fe   # FE supports wall friction
    assert "TURBULENCE MODEL : 3" in fe                    # FE keeps the resolved model
    assert "PRINTING CUMULATED FLOWRATES : YES" in fe
    assert "VARIABLE TIME-STEP : YES" in fe

    # opting into finite volumes: HLLC + constant viscosity, no FE-only keywords
    cfg.hydrodynamics.finite_volumes = True
    fv = write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=373.8,
                   turbulence_model=3).read_text()
    assert "EQUATIONS : 'SAINT-VENANT FV'" in fv
    assert "FINITE VOLUME SCHEME : 5" in fv and "FINITE VOLUME SCHEME SPACE ORDER : 2" in fv
    assert "TURBULENCE MODEL : 1" in fv                    # forced (constant viscosity)
    assert "SCHEME FOR ADVECTION OF VELOCITIES" not in fv
    assert "TIDAL FLATS" not in fv and "SOLVER :" not in fv
    assert "LAW OF FRICTION ON LATERAL BOUNDARIES" not in fv


def test_turbulence_model_auto_selection(tmp_path):
    """'auto' picks Smagorinski LES / k-epsilon / Spalart-Allmaras from the channel
    cell size relative to the turbulence length scale; an explicit setting overrides."""
    from hydromate.steering import select_turbulence_model

    cfg = _minimal_cfg(tmp_path)
    cfg.hydrodynamics.turbulence_length_scale = 1.0     # L = 1 m

    cfg.mesh.channel_size = 0.05                         # dx/L <= 0.2**1.5 -> >=80% TKE
    assert select_turbulence_model(cfg, None)[0] == 4   # Smagorinski LES
    cfg.mesh.channel_size = 0.2                          # ~5 cells/L
    assert select_turbulence_model(cfg, None)[0] == 3   # k-epsilon
    cfg.mesh.channel_size = 0.5                          # ~2 cells/L (coarse)
    assert select_turbulence_model(cfg, None)[0] == 6   # Spalart-Allmaras

    cfg.hydrodynamics.turbulence_model = "k-epsilon"    # explicit name overrides
    assert select_turbulence_model(cfg, None)[0] == 3
    cfg.hydrodynamics.turbulence_model = 6              # explicit int overrides
    assert select_turbulence_model(cfg, None)[0] == 6


def test_unsteady_cas_and_liquid_boundaries(tmp_path):
    """A hydrograph yields a liquid-boundaries file (Q(t)/SL(t)) and an unsteady
    cas referencing it with a DURATION; the steady writer is unaffected."""
    import numpy as np

    from hydromate import steering
    from hydromate.boundary import LiquidBoundary
    from hydromate.hydraulics import Inflow

    cfg = _minimal_cfg(tmp_path)
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=30),
               LiquidBoundary(index=2, kind="outflow", n_nodes=10),
               LiquidBoundary(index=3, kind="inflow", n_nodes=10)]
    inflow = Inflow(times_s=np.array([0.0, 3600.0, 7200.0]),
                    discharge=np.array([40.0, 60.0, 50.0]), steady_value=50.0)

    lb = steering.write_liquid_boundaries(cfg, liquids, inflow,
                                          outflow_wse_fn=lambda q: 373.0 + 0.01 * q)
    txt = lb.read_text()
    assert lb.name == cfg.liquid_boundaries_file
    header = txt.splitlines()[1].split()
    assert header == ["T", "Q(1)", "SL(2)", "Q(3)"]     # boundary-index order
    # total reach Q split across the two inflows by node share (30:10 of 40 at t=0)
    first = txt.splitlines()[3].split()
    assert float(first[1]) == 30.0 and float(first[3]) == 10.0

    cas = steering.write_cas(
        cfg, liquids, inflow_q=40.0, outflow_wse=373.4, turbulence_model=3,
        unsteady=True, liquid_boundaries_file=lb.name, duration=7200.0,
        out_name=cfg.unsteady_cas_file).read_text()
    assert f"LIQUID BOUNDARIES FILE : {cfg.liquid_boundaries_file}" in cas
    assert "DURATION : 7200.0" in cas and "NUMBER OF TIME STEPS" not in cas
    assert "TITLE : 'prewet-test unsteady'" in cas


def _minimal_cfg(tmp_path):
    from hydromate.config import (
        Calibration, Config, Friction, Hydrodynamics, Inputs, MeshConfig,
        Morphodynamics, TelemacEnv,
    )

    model = tmp_path / "model"
    model.mkdir()
    dummy = tmp_path / "dummy"
    dummy.write_text("")
    return Config(
        name="prewet-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        inputs=Inputs(dem_initial=dummy, boundary=dummy, liquid_boundaries=dummy,
                      inflow=dummy),
        mesh=MeshConfig(), friction=Friction(), hydrodynamics=Hydrodynamics(),
        morphodynamics=Morphodynamics(), calibration=Calibration(),
    )


def test_write_cas_warm_start_keywords(tmp_path):
    from hydromate import steering
    from hydromate.boundary import LiquidBoundary

    cfg = _minimal_cfg(tmp_path)
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=5),
               LiquidBoundary(index=2, kind="outflow", n_nodes=5)]

    # without pre-wetting: the analytical initial condition is used
    plain = steering.write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=379.5).read_text()
    assert "INITIAL CONDITIONS :" in plain
    assert "COMPUTATION CONTINUED" not in plain

    # with a warm-start file: continue the run from it, clock reset to zero.
    # Since TELEMAC 9.0 the PREVIOUS COMPUTATION FILE keyword alone triggers the
    # continuation (the boolean COMPUTATION CONTINUED keyword was removed).
    warm = steering.write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=379.5,
                              previous_computation="initial-conditions.slf").read_text()
    assert "COMPUTATION CONTINUED" not in warm
    assert "PREVIOUS COMPUTATION FILE : initial-conditions.slf" in warm
    assert "INITIAL TIME SET TO ZERO : YES" in warm
    assert "INITIAL CONDITIONS :" not in warm


def test_steady_state_auto_stop_is_opt_in_and_fixed_step_only(tmp_path):
    """The steady-state auto-stop is OFF by default. Even opted in it is suppressed
    with a variable time step (TELEMAC's absolute per-step STOP CRITERIA false-fires on
    the tiny CFL dt); it is emitted only for a steady, fixed-step run, never unsteady."""
    from hydromate import steering
    from hydromate.boundary import LiquidBoundary

    cfg = _minimal_cfg(tmp_path)
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=5),
               LiquidBoundary(index=2, kind="outflow", n_nodes=5)]

    # off by default
    default = steering.write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=379.5).read_text()
    assert "STOP IF A STEADY STATE IS REACHED" not in default

    # opted in but still variable time step -> suppressed (the unreliable case)
    cfg.hydrodynamics.stop_if_steady = True
    var = steering.write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=379.5).read_text()
    assert "STOP IF A STEADY STATE IS REACHED" not in var

    # opted in with a fixed time step -> emitted
    cfg.hydrodynamics.variable_timestep = False
    fixed = steering.write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=379.5).read_text()
    assert "STOP IF A STEADY STATE IS REACHED : YES" in fixed
    assert "STOP CRITERIA : 1.E-4;1.E-4;1.E-4" in fixed

    # never on the unsteady hydrograph run, even fixed-step + opted in
    uns = steering.write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=379.5,
                             unsteady=True, duration=3600.0,
                             liquid_boundaries_file="hg.liq",
                             out_name=cfg.unsteady_cas_file).read_text()
    assert "STOP IF A STEADY STATE IS REACHED" not in uns


def test_turbulence_solver_accuracy(tmp_path):
    """The turbulence-transport solver accuracy is loosened from TELEMAC's tight 1e-9
    default (ACCURACY OF K/EPSILON for k-epsilon, ACCURACY OF SPALART-ALLMARAS for S-A);
    constant viscosity / Smagorinski solve no transport equation, so none is written."""
    from hydromate import steering
    from hydromate.boundary import LiquidBoundary

    cfg = _minimal_cfg(tmp_path)
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=5),
               LiquidBoundary(index=2, kind="outflow", n_nodes=5)]

    keps = steering.write_cas(cfg, liquids, 47.0, 379.5, turbulence_model=3).read_text()
    assert "ACCURACY OF K : 1e-06" in keps and "ACCURACY OF EPSILON : 1e-06" in keps

    sa = steering.write_cas(cfg, liquids, 47.0, 379.5, turbulence_model=6).read_text()
    assert "ACCURACY OF SPALART-ALLMARAS : 1e-06" in sa and "ACCURACY OF K" not in sa

    const = steering.write_cas(cfg, liquids, 47.0, 379.5, turbulence_model=1).read_text()
    assert "ACCURACY OF" not in const


def test_variable_timestep_steady_run_bounded_by_duration(tmp_path):
    """A VARIABLE TIME-STEP steady run is bounded by DURATION (NUMBER OF TIME STEPS
    does not terminate a CFL-driven variable-dt run); a fixed-step run keeps using
    NUMBER OF TIME STEPS."""
    from hydromate import steering
    from hydromate.boundary import LiquidBoundary

    cfg = _minimal_cfg(tmp_path)
    cfg.hydrodynamics.n_time_steps = 15000
    cfg.hydrodynamics.time_step = 1.0
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=5),
               LiquidBoundary(index=2, kind="outflow", n_nodes=5)]

    var = steering.write_cas(cfg, liquids, 47.0, 379.5).read_text()
    assert "DURATION : 15000.0" in var and "NUMBER OF TIME STEPS" not in var

    cfg.hydrodynamics.variable_timestep = False
    fix = steering.write_cas(cfg, liquids, 47.0, 379.5).read_text()
    assert "NUMBER OF TIME STEPS : 15000" in fix and "DURATION" not in fix
