"""Unsteady (hydrograph) build tests: control-section parsing, the quasi-steady
telemac2d case hotstarted from the steady result, GAIA bedload/suspended coupling,
and the hydrograph-driven telemac3d case. No solver is involved (the steering
keywords are validated against the shipped dico separately)."""

from __future__ import annotations

import numpy as np
import pytest

from hydromate import boundary, selafin, threed, unsteady
from hydromate.boundary import LiquidBoundary


def _cli_row(code, n_global, rank, comment):
    c = code
    return (f"{c[0]} {c[1]} {c[2]}  0.000 0.000 0.000 0.000  2  0.000 0.000 0.000  "
            f"{n_global:>10}  {rank:>10}   # {comment}")


def _write_case_fixtures(cfg, *, hydrograph=True):
    """A minimal built case: geometry.slf, r2d.slf, boundaries.cli, the serialized
    liquid boundaries, and a (varying) inflow hydrograph CSV."""
    x = np.array([0.0, 4.0, 0.0, 4.0])
    y = np.array([0.0, 0.0, 4.0, 4.0])
    ikle = np.array([[1, 2, 3], [2, 4, 3]])
    ipobo = np.array([1, 2, 4, 3])
    selafin.write_geometry(cfg.model_path(cfg.geometry_slf), x=x, y=y, ikle=ikle,
                           ipobo=ipobo, bottom=np.zeros(4))
    selafin.write_initial_state(cfg.model_path(cfg.results_slf), x=x, y=y, ikle=ikle,
                                ipobo=ipobo, depth=np.ones(4), title="fake steady")

    rows = [_cli_row(boundary.INFLOW, 1, 1, "inflow"),
            _cli_row(boundary.INFLOW, 2, 2, "inflow"),
            _cli_row(boundary.WALL, 3, 3, ""),
            _cli_row(boundary.OUTFLOW_ELEV, 4, 4, "outflow"),
            _cli_row(boundary.OUTFLOW_ELEV, 5, 5, "outflow")]
    cfg.model_path(cfg.boundary_cli).write_text("\n".join(rows) + "\n")

    liquids = [LiquidBoundary(1, "inflow", 2), LiquidBoundary(2, "outflow", 2)]
    boundary.dump_liquid_boundaries(liquids, cfg.model_path(cfg.liquid_boundaries_json))

    qvals = [40, 40, 70, 50, 40] if hydrograph else [40, 40]
    inflow_csv = cfg.config_dir / "hydrograph.csv"
    inflow_csv.write_text("t,Q\n" + "".join(f"{i},{v}\n" for i, v in enumerate(qvals)))
    cfg.boundaries.inflow = inflow_csv


def test_write_control_sections(tmp_path):
    """Contiguous inflow/outflow node runs in the .cli become node-id sections."""
    cfg = _cfg(tmp_path)
    _write_case_fixtures(cfg)
    path = unsteady.write_control_sections(cfg)
    lines = path.read_text().splitlines()
    assert lines[1] == "2 -1"                      # 2 sections, node-id mode
    assert lines[2] == "inflow_1" and lines[3] == "1 2"    # nodes 1..2
    assert lines[4] == "outflow_1" and lines[5] == "4 5"   # nodes 4..5


def test_load_hydrograph_rejects_constant(tmp_path):
    cfg = _cfg(tmp_path)
    _write_case_fixtures(cfg, hydrograph=False)
    with pytest.raises(ValueError, match="single/constant discharge"):
        unsteady.load_hydrograph(cfg)


def test_build_unsteady_case_hotstarts_from_steady(tmp_path):
    """The unsteady2d case continues from r2d.slf, drives Q(t) from the liquid file,
    uses quasi-steady corrections + control sections, and writes a distinct result."""
    cfg = _cfg(tmp_path)
    _write_case_fixtures(cfg)
    s = unsteady.build_unsteady_case(cfg)
    txt = s.cas.read_text()

    assert f"PREVIOUS COMPUTATION FILE : {cfg.results_slf}" in txt
    assert "INITIAL TIME SET TO ZERO : YES" in txt
    assert f"LIQUID BOUNDARIES FILE : {cfg.liquid_boundaries_file}" in txt
    assert "DURATION :" in txt and "NUMBER OF TIME STEPS" not in txt
    assert "NUMBER OF CORRECTIONS OF DISTRIBUTIVE SCHEMES : 2" in txt
    assert f"SECTIONS INPUT FILE : {cfg.control_sections_file}" in txt
    assert f"RESULTS FILE : {cfg.results_unsteady_slf}" in txt   # not r2d.slf
    assert s.cas.name == cfg.unsteady_cas_file
    assert s.n_inflows == 1 and s.n_outflows == 1
    # the liquid file carries Q(1) for the inflow and SL(2) for the outflow
    header = s.liquid_boundaries.read_text().splitlines()[1].split()
    assert header == ["T", "Q(1)", "SL(2)"]


def test_build_unsteady_case_couples_gaia_bedload_and_suspended(tmp_path):
    cfg = _cfg(tmp_path)
    _write_case_fixtures(cfg)
    cfg.morphodynamics.enabled = True
    cfg.morphodynamics.bedload = True
    cfg.morphodynamics.suspended_load = True
    cfg.morphodynamics.sediment_classes = [{"diameter": 0.0008, "density": 2650}]

    s = unsteady.build_unsteady_case(cfg)
    cas = s.cas.read_text()
    assert "COUPLING WITH : 'GAIA'" in cas
    assert f"GAIA STEERING FILE : {cfg.gaia_cas}" in cas
    gaia = s.gaia_cas.read_text()
    assert "BED LOAD FOR ALL SANDS : YES" in gaia
    assert "BED-LOAD TRANSPORT FORMULA FOR ALL SANDS : 1" in gaia
    assert "SUSPENSION FOR ALL SANDS : YES" in gaia


def test_build_unsteady_3d_case(tmp_path):
    """The 3D unsteady case reuses the hydrograph forcing on a distinct cas/result."""
    cfg = _cfg(tmp_path)
    _write_case_fixtures(cfg)
    s = unsteady.build_unsteady_3d_case(cfg)
    txt = s.cas.read_text()
    assert s.cas.name == threed.cas3d_name(cfg, "-unsteady")
    assert "NON-HYDROSTATIC VERSION : YES" in txt
    assert f"LIQUID BOUNDARIES FILE : {cfg.liquid_boundaries_file}" in txt
    assert f"3D RESULT FILE : {cfg.results3d_unsteady_slf}" in txt
    # the step count spans the hydrograph duration (clamped to the builder's floor)
    assert s.n_time_steps * s.time_step >= s.duration
    assert s.liquid_boundaries.exists()


def _cfg(tmp_path):
    from hydromate.config import (
        Boundaries, Calibration, Config, Friction, Geodata, GroundTruth,
        Hydrodynamics, Initialization, MeshConfig, Morphodynamics, TelemacEnv,
    )

    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    dummy = tmp_path / "dummy"
    dummy.write_text("")
    return Config(
        name="unsteady-test", crs_epsg=25832, config_dir=tmp_path,
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
