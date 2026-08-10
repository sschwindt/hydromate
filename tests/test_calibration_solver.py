"""Which solver / case HydroBayesCal is pointed at (``calibration.solver_name``).

The 3D profile calibration (Inn KB15, per-depth ADV velocities) drives telemac3d
against the 3D steering file, so the emitted ``config_Telemac.py`` must not be
hard-wired to the built 2D case the way it used to be.
"""

from __future__ import annotations

import pytest

from hydromate.calibration import emit_hbc_config


@pytest.fixture
def cfg(tmp_path):
    from hydromate.config import (
        Boundaries, Calibration, Config, Friction, Geodata, GroundTruth,
        Hydrodynamics, Initialization, MeshConfig, Morphodynamics, TelemacEnv,
    )

    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    dummy = tmp_path / "dummy"
    dummy.write_text("")
    return Config(
        name="solver-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy),
        boundaries=Boundaries(liquid_boundaries=dummy, outflow_condition="elevation",
                              prescribed_elevation=373.5),
        initialization=Initialization(),
        mesh=MeshConfig(), friction=Friction(), hydrodynamics=Hydrodynamics(),
        morphodynamics=Morphodynamics(), ground_truth=GroundTruth(),
        calibration=Calibration(),
    )


def _emit(cfg, **kw):
    for k, v in kw.items():
        setattr(cfg.calibration, k, v)
    return emit_hbc_config(cfg, None).read_text()


def test_defaults_to_the_built_2d_case(cfg):
    text = _emit(cfg)
    assert "'solver_name': 'Telemac2d'" in text
    assert f"'control_file': {cfg.cas_file!r}" in text
    assert "'results_filename_base': 'r2d'" in text


def test_three_d_calibration_targets_the_3d_case(cfg):
    """Telemac3d + the 3D steering file. The results base must be the 3D one:
    HydroBayesCal sets '3D RESULT FILE' from it and derives the companion
    depth-averaged file as <base>_<id>_2d.slf."""
    text = _emit(cfg, solver_name="Telemac3d",
                 control_file="hotstart3d_hydrodyn.cas")
    assert "'solver_name': 'Telemac3d'" in text
    assert "'control_file': 'hotstart3d_hydrodyn.cas'" in text
    assert "'results_filename_base': 'r3d'" in text


def test_explicit_results_base_wins(cfg):
    text = _emit(cfg, solver_name="Telemac3d", results_base="r3d-cal")
    assert "'results_filename_base': 'r3d-cal'" in text
