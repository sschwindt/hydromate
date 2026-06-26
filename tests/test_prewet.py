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


def test_steering_finite_volume_vs_finite_element(tmp_path):
    """Default steering is finite volumes (HLLC + constant viscosity, no FE-only
    keywords); finite_volumes=False emits the FE numerics instead. FV forces
    turbulence model 1 even if another is configured."""
    from hydromate.boundary import LiquidBoundary
    from hydromate.steering import write_cas

    cfg = _minimal_cfg(tmp_path)
    liquids = [LiquidBoundary(index=1, kind="inflow", n_nodes=10),
               LiquidBoundary(index=2, kind="outflow", n_nodes=10)]

    cfg.hydrodynamics.turbulence_model = 3            # FV must override this to 1
    fv = write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=373.8).read_text()
    assert "EQUATIONS : 'SAINT-VENANT FV'" in fv
    assert "FINITE VOLUME SCHEME : 5" in fv and "FINITE VOLUME SCHEME SPACE ORDER : 2" in fv
    assert "TURBULENCE MODEL : 1" in fv                # forced (constant viscosity)
    assert "FREE SURFACE GRADIENT COMPATIBILITY : 0.1" in fv
    assert "PRINTING CUMULATED FLOWRATES : YES" in fv
    assert "VARIABLE TIME-STEP : YES" in fv
    # FE-only keywords must be absent under finite volumes (incl. lateral-boundary
    # wall friction, which the FV kernel rejects)
    assert "SCHEME FOR ADVECTION OF VELOCITIES" not in fv
    assert "TIDAL FLATS" not in fv and "SOLVER :" not in fv
    assert "LAW OF FRICTION ON LATERAL BOUNDARIES" not in fv

    cfg.hydrodynamics.finite_volumes = False
    fe = write_cas(cfg, liquids, inflow_q=47.0, outflow_wse=373.8).read_text()
    assert "SAINT-VENANT FV" not in fe
    assert "SCHEME FOR ADVECTION OF VELOCITIES : 14" in fe
    assert "TIDAL FLATS : YES" in fe and "SOLVER : 2" in fe
    assert "LAW OF FRICTION ON LATERAL BOUNDARIES" in fe   # FE supports wall friction
    assert "TURBULENCE MODEL : 3" in fe                # FE keeps the configured model


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
