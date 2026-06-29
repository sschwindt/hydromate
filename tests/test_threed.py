"""TELEMAC-3D extension (add3d.py) tests: vertical-layer inference, the Courant
time step, the 2D->3D turbulence mapping, and the generated steering file. The
TELEMAC solver is not involved (the steering keywords are validated against the
shipped telemac3d.dico separately, outside the test suite)."""

from __future__ import annotations

import numpy as np

from hydromate import selafin
from hydromate import threed


def _fake_2d_data(edge: float, depth: float):
    """A 2-triangle square of side *edge* with uniform water *depth* everywhere."""
    x = np.array([0.0, edge, 0.0, edge])
    y = np.array([0.0, 0.0, edge, edge])
    ikle = np.array([[0, 1, 2], [1, 3, 2]])
    return {
        "x": x, "y": y, "ikle": ikle, "var_names": ["WATER DEPTH"],
        "values": {"WATER DEPTH": np.full(4, depth)},
    }


def test_infer_layers_targets_aspect_two(tmp_path):
    """At dx~edge and a comfortable depth the level count targets dz~dx/2."""
    vd = threed.infer_vertical_layers(_cfg(tmp_path), _fake_2d_data(edge=1.0, depth=2.0))
    assert vd.dx == 1.0 and vd.depth == 2.0
    assert vd.n_levels == 5                      # round(2 / (1/2)) + 1, clamped
    assert abs(vd.dz - 0.5) < 1e-9               # 2 m / (5-1)
    assert vd.aspect <= threed.MAX_ASPECT        # dz never finer than dx/4


def test_infer_layers_respects_max_aspect(tmp_path):
    """Flat, wide cells (dx >> h) are capped so dz stays >= dx/4 (max 4x finer)."""
    vd = threed.infer_vertical_layers(_cfg(tmp_path), _fake_2d_data(edge=2.0, depth=0.5))
    assert vd.n_levels == 2                       # aspect cap binds below N_LEVELS_MIN
    assert vd.aspect <= threed.MAX_ASPECT + 1e-9  # dz = dx/4 exactly here
    assert vd.dz >= vd.dx / threed.MAX_ASPECT - 1e-9


def test_courant_time_step_uses_smallest_cell():
    # dt = 0.6 * min(dx, dz) / u = 0.6 * 0.5 / 2 = 0.15
    assert abs(threed.courant_time_step(dx=1.0, dz=0.5, u_char=2.0) - 0.15) < 1e-9


def test_turbulence_mapping_2d_to_3d(tmp_path):
    """The 2D selection maps onto the 3D numbering (S-A is 5 in 3D, not 6)."""
    cfg = _cfg(tmp_path)
    cfg.hydrodynamics.turbulence_length_scale = 1.0

    cfg.mesh.channel_size = 0.05                  # fine -> Smagorinski (4) both dims H
    assert threed.select_3d_turbulence(cfg, None)[:2] == (4, 2)
    cfg.mesh.channel_size = 0.2                    # moderate -> k-epsilon (3 H, 3 V)
    assert threed.select_3d_turbulence(cfg, None)[:2] == (3, 3)
    cfg.mesh.channel_size = 0.5                    # coarse -> Spalart-Allmaras (5 H+V)
    assert threed.select_3d_turbulence(cfg, None)[:2] == (5, 5)  # S-A: both directions


def test_build_3d_cas_from_2d_result(tmp_path):
    """build_3d_cas reads a 2D result and writes a hotstarted non-hydrostatic case."""
    cfg = _cfg(tmp_path)
    # a small 2D result file (r2d.slf) the 3D case hotstarts from
    x = np.array([0.0, 4.0, 0.0, 4.0])
    y = np.array([0.0, 0.0, 4.0, 4.0])
    selafin.write_initial_state(
        cfg.model_path(cfg.results_slf), x=x, y=y,
        ikle=np.array([[1, 2, 3], [2, 4, 3]]), ipobo=np.array([1, 2, 4, 3]),
        depth=np.array([1.0, 1.0, 1.0, 1.0]), title="fake 2d",
    )

    setup = threed.build_3d_cas(cfg)
    assert setup.cas.name == "threed-test3d.cas"
    txt = setup.cas.read_text()
    assert "NON-HYDROSTATIC VERSION : YES" in txt
    assert f"NUMBER OF HORIZONTAL LEVELS : {setup.n_levels}" in txt
    assert "MESH TRANSFORMATION : 1" in txt
    assert f"FILE FOR 2D CONTINUATION : {cfg.results_slf}" in txt
    assert "INITIAL TIME SET TO ZERO : YES" in txt
    assert f"HORIZONTAL TURBULENCE MODEL : {setup.h_turbulence}" in txt
    assert "FRICTION COEFFICIENT FOR THE BOTTOM" in txt   # global (no zonal file)
    assert "FINITE VOLUME" not in txt and "SAINT-VENANT FV" not in txt
    assert f"TIME STEP : {setup.time_step}" in txt
    assert setup.courant == 0.6


def _cfg(tmp_path):
    from hydromate.config import (
        Calibration, Config, Friction, Hydrodynamics, Inputs, MeshConfig,
        Morphodynamics, TelemacEnv,
    )

    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    dummy = tmp_path / "dummy"
    dummy.write_text("")
    return Config(
        name="threed-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        inputs=Inputs(dem_initial=dummy, boundary=dummy, liquid_boundaries=dummy,
                      inflow=dummy),
        mesh=MeshConfig(), friction=Friction(), hydrodynamics=Hydrodynamics(),
        morphodynamics=Morphodynamics(), calibration=Calibration(),
    )
