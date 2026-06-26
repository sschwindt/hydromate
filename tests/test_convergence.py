"""SELAFIN reader round-trip and mesh-convergence report maths.

* ``selafin.read_slf`` reads back exactly what ``write_geometry`` wrote (the only
  reader we have), incl. BOTTOM / FRIC_ID / BOTTOM FRICTION variables.
* the convergence report computes relative change, observed order and GCI, and
  the converged verdict, on synthetic levels — and ``run_mesh_convergence`` drives
  an injected (fake) TELEMAC simulator so the orchestration is exercised without
  the solver.

Pure-python (numpy/scipy). Run via:
    mamba run -n hydromate-env pytest tests/test_convergence.py
"""

from __future__ import annotations

import numpy as np

from hydromate import convergence, percent_levels, selafin
from hydromate.convergence import LevelResult, build_report


def _rt_level(label, cell, depth, vel, rt):
    n = int(2e5 / cell ** 2)
    return LevelResult(label, cell, n, n // 2, cell * 0.7, cell * 0.1, rt,
                       {"WATER DEPTH": np.full(6, depth),
                        "SCALAR VELOCITY": np.full(6, vel)})


def test_percent_levels():
    class _Mesh:
        channel_size, floodplain_size = 1.0, 3.0

    class _Cfg:
        mesh = _Mesh()

    lv = percent_levels(_Cfg())
    assert [round(x[1], 2) for x in lv] == [1.4, 1.2, 1.0, 0.8, 0.6]   # coarse->fine
    assert "coarser" in lv[0][0] and lv[2][0] == "baseline" and "finer" in lv[-1][0]


def test_recommendation_and_xlsx(tmp_path):
    # 5 meshes (coarse->fine) with a converging water depth / velocity
    levels = [_rt_level("+40% coarser", 1.4, 1.20, 0.80, 20),
              _rt_level("+20% coarser", 1.2, 1.08, 0.86, 35),
              _rt_level("baseline", 1.0, 1.03, 0.89, 60),
              _rt_level("-20% finer", 0.8, 1.015, 0.90, 120),
              _rt_level("-40% finer", 0.6, 1.012, 0.905, 260)]
    rep = build_report(levels, ["WATER DEPTH", "SCALAR VELOCITY"], 0.02,
                       np.zeros((6, 2)))
    rep.case = "inn"
    rec = rep.recommendation()
    # the baseline is the coarsest mesh within tolerance under refinement
    assert rec["converged"] and rec["cell_size"] == 1.0
    assert rec["speedup_vs_finest"] > 1.0

    out = rep.to_xlsx(tmp_path / "report.xlsx")
    assert out.exists()
    import openpyxl
    ws = openpyxl.load_workbook(out).active
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "RECOMMENDATION" in text and "cell size = 1.000 m" in text
    assert "+40% coarser" in text and "-40% finer" in text


def test_selafin_read_roundtrip(tmp_path):
    x = np.array([0.0, 1.0, 1.0, 0.0])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    ikle = np.array([[1, 2, 3], [1, 3, 4]])           # 1-based
    ipobo = np.array([1, 2, 3, 4])
    bottom = np.array([10.0, 11.0, 12.0, 13.0])
    fric = np.array([1, 1, 2, 2])
    rough = np.array([0.2, 0.2, 0.5, 0.5])
    p = selafin.write_geometry(tmp_path / "g.slf", x=x, y=y, ikle=ikle, ipobo=ipobo,
                               bottom=bottom, friction_id=fric, roughness=rough)

    d = selafin.read_slf(p)
    np.testing.assert_allclose(d["x"], x)
    np.testing.assert_allclose(d["y"], y)
    np.testing.assert_array_equal(d["ikle"], ikle - 1)           # reader is 0-based
    assert d["var_names"] == ["BOTTOM", "FRIC_ID", "BOTTOM FRICTION"]
    np.testing.assert_allclose(d["values"]["BOTTOM"], bottom)
    np.testing.assert_allclose(d["values"]["BOTTOM FRICTION"], rough)
    np.testing.assert_allclose(d["values"]["FRIC_ID"], fric)
    assert d["time"] == 0.0 and d["n_times"] == 1


def _level(label, cell, depth, vel, n_probes=6):
    return LevelResult(
        label=label, cell_size=cell, n_elem=int(1000 / cell), n_nodes=int(600 / cell),
        min_edge=cell * 0.7, dt=cell * 0.1, runtime_s=cell,
        qoi={"WATER DEPTH": np.full(n_probes, depth),
             "SCALAR VELOCITY": np.full(n_probes, vel)},
    )


def test_relative_change_and_verdict():
    # depth converges (1.20 -> 1.05 -> 1.02), velocity already settled
    levels = [_level("coarse", 2.0, 1.20, 0.80),
              _level("medium", 2.0 / np.sqrt(2), 1.05, 0.802),
              _level("fine", 1.0, 1.02, 0.803)]
    rep = build_report(levels, ["WATER DEPTH", "SCALAR VELOCITY"], 0.02,
                       np.zeros((6, 2)))
    # finest relative change for depth = |1.02-1.05|/1.02 ~ 2.94% > 2% -> not converged
    assert abs(rep.rel_change["WATER DEPTH"][-1] - 0.03 / 1.02) < 1e-6
    assert rep.converged is False
    # a looser 5% tolerance converges
    rep5 = build_report(levels, ["WATER DEPTH", "SCALAR VELOCITY"], 0.05,
                        np.zeros((6, 2)))
    assert rep5.converged is True
    # monotone depth -> a finite positive observed order and a GCI
    assert rep.observed_order["WATER DEPTH"] is not None
    assert rep.observed_order["WATER DEPTH"] > 0
    assert rep.gci_fine["WATER DEPTH"] is not None
    assert "CONVERGED" not in rep.format() or "NOT converged" in rep.format()


def test_run_mesh_convergence_with_fake_solver():
    probes = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 0.5]])

    # fake simulator: QoI = exact + C * cell_size**2 (2nd-order, converging)
    def simulate(label, ch, fp, pts, quantities):
        depth = 1.0 + 0.05 * ch ** 2
        vel = 0.9 + 0.02 * ch ** 2
        qoi = {"WATER DEPTH": np.full(len(pts), depth),
               "SCALAR VELOCITY": np.full(len(pts), vel)}
        return LevelResult(label, ch, int(2000 / ch), int(1200 / ch), ch * 0.7,
                           ch * 0.1, ch, qoi)

    rep = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes, tolerance=0.02,
        simulate=simulate,
        levels=[("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)],
    )
    assert [lv.label for lv in rep.levels] == ["coarse", "medium", "fine"]
    # 2nd-order field -> observed order near 2
    assert 1.5 < rep.observed_order["WATER DEPTH"] < 2.5
    # successive changes shrink
    assert rep.rel_change["WATER DEPTH"][1] < rep.rel_change["WATER DEPTH"][0]
    assert "Mesh-convergence study" in rep.format()


class _DummyCfg:
    """Minimal stand-in so default_probes/base_dir aren't reached (probes given)."""

    def postprocessing_path(self, name):  # pragma: no cover - base_dir unused here
        import tempfile
        from pathlib import Path
        return Path(tempfile.mkdtemp()) / name


if __name__ == "__main__":
    test_relative_change_and_verdict()
    print("CONVERGENCE TESTS PASSED")
