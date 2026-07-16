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

import json

import numpy as np

from hydromate import convergence, percent_levels, ratio_levels, selafin
from hydromate.convergence import LevelResult, build_report, estimate_next_runtime


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


def test_ratio_levels():
    class _Mesh:
        channel_size, floodplain_size = 1.0, 3.0
        size_scale = 1.0

    class _Cfg:
        mesh = _Mesh()

    lv = ratio_levels(_Cfg(), ratio=1.3)               # 1 coarser + baseline + 2 finer
    assert [round(x[1], 4) for x in lv] == [1.3, 1.0, round(1 / 1.3, 4),
                                            round(1 / 1.69, 4)]
    assert [round(x[2], 4) for x in lv] == [3.9, 3.0, round(3 / 1.3, 4),
                                            round(3 / 1.69, 4)]
    assert "coarser" in lv[0][0] and lv[1][0] == "baseline"
    assert lv[2][0] == "finer x1.30" and lv[3][0] == "finer x1.69"
    import pytest

    with pytest.raises(ValueError):
        ratio_levels(_Cfg(), ratio=1.0)                # r must exceed 1 (Celik: >=1.3)


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


def _order2_level(label, cell):
    """Clean 2nd-order data on any ladder: value = exact + C * cell**2."""
    return _level(label, cell, 1.0 + 0.05 * cell ** 2, 0.9 + 0.02 * cell ** 2)


def test_gci_verdict_and_asymptotic():
    # 4 meshes on an r=1.3 ladder with clean 2nd-order data: the finest triplet's
    # asymptotic-range check passes, the verdict is GCI-based, converged at 5%
    sizes = [1.69, 1.3, 1.0, 1.0 / 1.3]
    levels = [_order2_level(f"L{i}", c) for i, c in enumerate(sizes)]
    rep = build_report(levels, ["WATER DEPTH", "SCALAR VELOCITY"], 0.05,
                       np.zeros((6, 2)))
    assert rep.verdict_basis == "gci"
    for q in rep.quantities:
        assert abs(rep.asymptotic_ratio[q] - 1.0) < 0.1
        assert rep.gci_med[q] is not None
    assert rep.converged is True
    assert "GCI of the finest grid triplet" in rep.format()

    # oscillating data: no order computable -> relative-change fallback basis
    noisy = [_level("L0", 1.69, 1.20, 0.8), _level("L1", 1.3, 1.00, 0.8),
             _level("L2", 1.0, 1.10, 0.8), _level("L3", 1.0 / 1.3, 1.05, 0.8)]
    rep_noisy = build_report(noisy, ["WATER DEPTH"], 0.05, np.zeros((6, 2)))
    assert rep_noisy.verdict_basis == "rel_change"
    assert rep_noisy.gci_fine["WATER DEPTH"] is None


def test_p_clamped_for_gci():
    # e21/e32 = 6 on an r=1.3 ladder -> raw p = ln6/ln1.3 ~ 6.8 (the ering noise
    # pathology); the GCI must use the clamped p, not the absurd raw order
    levels = [_level("coarse", 1.69, 1.30, 0.8), _level("med", 1.3, 1.06, 0.8),
              _level("fine", 1.0, 1.02, 0.8)]
    rep = build_report(levels, ["WATER DEPTH"], 0.05, np.zeros((6, 2)))
    p, pu = rep.observed_order["WATER DEPTH"], rep.order_used["WATER DEPTH"]
    assert p > 2.5
    assert pu == convergence.THEORETICAL_ORDER
    expect = 1.25 * (0.04 / 1.02) / (1.3 ** 2 - 1.0)
    assert abs(rep.gci_fine["WATER DEPTH"] - expect) < 1e-9


def test_estimate_next_runtime():
    # runtimes following t = 100 * dx**-3 -> the log-log fit recovers the exponent
    levels = [_rt_level("a", 2.0, 1.0, 0.8, 100 * 2.0 ** -3),
              _rt_level("b", 1.4, 1.0, 0.8, 100 * 1.4 ** -3),
              _rt_level("c", 1.0, 1.0, 0.8, 100.0)]
    est, basis = estimate_next_runtime(levels, 1.0 / 1.3)
    assert "fitted" in basis
    assert abs(est - 100 * (1.0 / 1.3) ** -3) < 1.0
    # a single runtime -> theoretical dx^-3 fallback from the finest level
    est1, basis1 = estimate_next_runtime([levels[-1]], 0.5)
    assert "theoretical" in basis1 and abs(est1 - 800.0) < 1e-6
    # no runtimes recorded (injected test simulators) -> None
    lv = _level("x", 1.0, 1.0, 0.8)
    lv.runtime_s = None
    assert estimate_next_runtime([lv], 0.5)[0] is None


def _fake_simulate(label, ch, fp, pts, quantities):
    qoi = {"WATER DEPTH": np.full(len(pts), 1.0 + 0.05 * ch ** 2),
           "SCALAR VELOCITY": np.full(len(pts), 0.9 + 0.02 * ch ** 2)}
    return LevelResult(label, ch, int(2000 / ch), int(1200 / ch), ch * 0.7,
                       ch * 0.1, ch, qoi)


def test_extension_flow(tmp_path):
    probes = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 0.5]])
    prompts, answers = [], [True, False]

    def ask(prompt, default=False):
        prompts.append(prompt)
        return answers.pop(0)

    rep = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes, tolerance=1e-4,  # unreachable
        simulate=_fake_simulate, base_dir=tmp_path,
        levels=[("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)],
        extend_ratio=1.3, ask=ask,
    )
    # one extension accepted, the second declined -> exactly one extra level
    assert len(rep.levels) == 4
    assert rep.levels[-1].label == "finer x2.60"     # 2.0 / (1.0/1.3)
    assert abs(rep.levels[-1].cell_size - 1.0 / 1.3) < 1e-9
    assert len(prompts) == 2
    assert "estimated runtime" in prompts[0] and "NOT converged" in prompts[0]

    # declining immediately runs no extra level
    rep2 = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes, tolerance=1e-4,
        simulate=_fake_simulate, base_dir=tmp_path,
        levels=[("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)],
        extend_ratio=1.3, ask=lambda prompt, default=False: False,
    )
    assert len(rep2.levels) == 3


def test_auto_extend_refines_until_converged(tmp_path):
    # QoI is constant (1.0) once the cell size drops to <= 1.0, so the first
    # refinement below the baseline drives the successive relative change to 0.
    def simulate(label, ch, fp, pts, quantities):
        val = 1.0 + max(0.0, ch - 1.0)
        qoi = {"WATER DEPTH": np.full(len(pts), val),
               "SCALAR VELOCITY": np.full(len(pts), val)}
        return LevelResult(label, ch, int(2000 / ch), int(1200 / ch), ch * 0.7,
                           ch * 0.1, ch, qoi)

    probes = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 0.5]])
    # no ask= passed: auto_extend must auto-approve the extension (pytest stdin is
    # not a tty, so without auto_extend the default would decline and not converge)
    rep = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes, tolerance=0.05,
        simulate=simulate, base_dir=tmp_path,
        levels=[("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)],
        extend_ratio=1.3, max_extra_levels=8, auto_extend=True,
    )
    assert rep.converged
    assert len(rep.levels) == 4          # one extension was enough, then it stopped


def test_auto_extend_bounded_by_max_extra_levels(tmp_path):
    # never converges (unreachable tolerance) -> auto-refinement stops at the cap
    rep = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes_lin(), tolerance=1e-12,
        simulate=_fake_simulate, base_dir=tmp_path,
        levels=[("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)],
        extend_ratio=1.3, max_extra_levels=2, auto_extend=True,
    )
    assert not rep.converged
    assert len(rep.levels) == 5          # 3 base + 2 extra (the cap)


def test_auto_extend_stops_gracefully_on_failed_level(tmp_path):
    # a refinement that cannot be built/run (e.g. BAMG gives up) must not crash the
    # study: the extension stops and the report covers the completed levels.
    def simulate(label, ch, fp, pts, quantities):
        if ch < 1.0:                     # any refinement below baseline fails
            raise RuntimeError("BAMG failed")
        return _fake_simulate(label, ch, fp, pts, quantities)

    rep = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes_lin(), tolerance=1e-12,
        simulate=simulate, base_dir=tmp_path,
        levels=[("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)],
        extend_ratio=1.3, max_extra_levels=5, auto_extend=True,
    )
    assert len(rep.levels) == 3          # no extra level survived; no exception raised


def probes_lin():
    return np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 0.5]])


def _write_fake_result(level_dir, depth_value):
    """A real (tiny) result SELAFIN + level.json for the resume scan."""
    model = level_dir / "model"
    model.mkdir(parents=True)
    x = np.array([0.0, 2.0, 2.0, 0.0])
    y = np.array([0.0, 0.0, 2.0, 2.0])
    ikle = np.array([[1, 2, 3], [1, 3, 4]])
    ipobo = np.array([1, 2, 3, 4])
    selafin.write_initial_state(model / "r2d.slf", x, y, ikle, ipobo,
                                depth=np.full(4, depth_value),
                                velocity_u=np.full(4, 0.8),
                                velocity_v=np.zeros(4))


def test_resume(tmp_path):
    probes = np.array([[0.5, 0.5], [1.0, 1.0]])
    coarse_dir = tmp_path / "coarse"
    _write_fake_result(coarse_dir, depth_value=1.2)
    (coarse_dir / "level.json").write_text(json.dumps(
        {"label": "coarse", "channel_size": 2.0, "floodplain_size": 6.0,
         "scale": 2.0, "cell_size": 2.0, "n_elem": 2, "n_nodes": 4,
         "min_edge": 1.4, "dt": 0.2, "runtime_s": 111.0}))

    ran = []

    def simulate(label, ch, fp, pts, quantities):
        ran.append(label)
        return _fake_simulate(label, ch, fp, pts, quantities)

    levels = [("coarse", 2.0, 6.0), ("medium", 1.4, 4.2), ("fine", 1.0, 3.0)]
    rep = convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes, simulate=simulate,
        base_dir=tmp_path, levels=levels,
        ask=lambda prompt, default=False: True,      # accept the resume offer
    )
    # the completed level was reused (QoI re-extracted from its r2d.slf)
    assert ran == ["medium", "fine"]
    reused = rep.levels[0]
    assert reused.runtime_s == 111.0 and reused.scale == 2.0
    np.testing.assert_allclose(reused.qoi["WATER DEPTH"], 1.2)
    np.testing.assert_allclose(reused.qoi["SCALAR VELOCITY"], 0.8)

    # a size mismatch in level.json invalidates the stored level -> re-run
    meta = json.loads((coarse_dir / "level.json").read_text())
    meta["channel_size"] = 2.5
    (coarse_dir / "level.json").write_text(json.dumps(meta))
    ran.clear()
    convergence.run_mesh_convergence(
        _DummyCfg(), discharge=47.0, probes=probes, simulate=simulate,
        base_dir=tmp_path, levels=levels,
        ask=lambda prompt, default=False: True,
    )
    assert ran == ["coarse", "medium", "fine"]


def test_geom_stats_ignores_zero_length_edges():
    # node 3 duplicates node 0 (merged parallel results do this at subdomain
    # interfaces); the zero-length edge 0-3 must not poison min_edge/dt
    data = {"x": np.array([0.0, 1.0, 0.0, 0.0]),
            "y": np.array([0.0, 0.0, 1.0, 0.0]),
            "ikle": np.array([[0, 1, 2], [0, 3, 1]]),
            "values": {}}
    min_edge, depth, vel = convergence._geom_stats(data, np.array([[0.3, 0.3]]))
    assert min_edge == 1.0
    assert np.isfinite(convergence._cfl_dt(min_edge, depth, vel))


def test_write_readme_from_report(tmp_path):
    levels = [_rt_level("coarser x1.30", 1.3, 1.20, 0.80, 20),
              _rt_level("baseline", 1.0, 1.03, 0.89, 60),
              _rt_level("finer x1.30", 1 / 1.3, 1.015, 0.90, 120),
              _rt_level("finer x1.69", 1 / 1.69, 1.012, 0.905, 260)]
    rep = build_report(levels, ["WATER DEPTH", "SCALAR VELOCITY"], 0.05,
                       np.zeros((6, 2)))
    rep.case = "inn"
    out = convergence.write_readme(tmp_path, report=rep)
    assert out == tmp_path / "README.md"
    text = out.read_text()
    # the reviewer-requested pointers: central xlsx + what it compares
    assert "mesh-convergence.xlsx" in text
    assert ("compares them with 3 additional simulations: 1 performed on a "
            "coarser mesh and 2 performed on successively refined meshes") in text
    # governing equations, quantities, criteria, file inventory, packaging
    assert "shallow water" in text and "TELEMAC-2D" in text
    assert "water depth h [m]" in text and "sampled at 6 fixed probe points" in text
    assert "Celik" in text and "Grid" in text and "tolerance: 5%" in text
    assert "| baseline | 1.000 |" in text and "| finer x1.69 |" in text
    assert "geometry.slf" in text and "r2d.slf" in text and "tar czf" in text
    # the README is deliberately tool-agnostic
    assert "hydromate" not in text.lower()


def test_write_readme_scan_mode(tmp_path):
    # no report: the levels are recovered from the per-level level.json files
    for lbl, cs, ne in (("baseline", 0.40, 75919), ("coarser x1.30", 0.52, 45873),
                        ("finer x1.30", 0.31, 119960)):
        d = tmp_path / convergence._slug(lbl)
        d.mkdir()
        (d / "level.json").write_text(json.dumps(
            {"label": lbl, "channel_size": cs, "floodplain_size": 3 * cs,
             "scale": 1.0, "cell_size": cs, "n_elem": ne, "n_nodes": ne // 2,
             "min_edge": 0.03, "dt": 0.01, "runtime_s": 100.0}))
    out = convergence.write_readme(
        tmp_path, case="ering", tolerance=0.05, n_probes=40,
        turbulence="Spalart-Allmaras",
        extra_entries={"old/": "a superseded earlier study run"})
    text = out.read_text()
    assert "case 'ering'" in text and "sampled at 40 fixed probe points" in text
    assert ("compares them with 2 additional simulations: 1 performed on a "
            "coarser mesh and 1 performed on successively refined meshes") in text
    # rows come out coarse -> fine
    assert text.index("| coarser x1.30 |") < text.index("| baseline |") \
        < text.index("| finer x1.30 |")
    assert "Spalart-Allmaras turbulence closure" in text
    assert "`old/`: a superseded earlier study run" in text
    assert "hydromate" not in text.lower()


if __name__ == "__main__":
    test_relative_change_and_verdict()
    print("CONVERGENCE TESTS PASSED")
