"""Vertical-layer (3D) convergence report maths + orchestration with a fake solver.

The layer ladder, the relative-change / order / GCI maths, the recommendation
(fewest grid-independent layers) and the styled xlsx are exercised without the
TELEMAC solver by injecting a ``simulate`` callable (as the 2D study does).
"""

from __future__ import annotations

import numpy as np

from hydromate import vertical_convergence as vc
from hydromate.vertical_convergence import VerticalLevel, _build_report


def _level(n_levels, depth, vel, runtime=None, n_probes=6):
    dz = 0.9 / (n_levels - 1)
    return VerticalLevel(
        n_levels=n_levels, dz=dz, n_nodes3d=1000 * n_levels,
        n_elem3d=1800 * (n_levels - 1), time_step=round(0.6 * dz, 4),
        runtime_s=runtime if runtime is not None else float(n_levels),
        qoi={"WATER DEPTH": np.full(n_probes, depth),
             "SCALAR VELOCITY": np.full(n_probes, vel)})


def test_layer_levels_auto_ladder():
    # baseline inference -> half / baseline / double / triple of the intervals.
    # fake a 2D result with depth ~1 m and unit cells so infer_vertical_layers
    # returns a known baseline; here just assert the ladder is ascending + unique.
    data = {
        "x": np.array([0.0, 1.0, 0.0, 1.0]), "y": np.array([0.0, 0.0, 1.0, 1.0]),
        "ikle": np.array([[0, 1, 2], [1, 3, 2]]),
        "var_names": ["WATER DEPTH"], "values": {"WATER DEPTH": np.full(4, 2.0)},
    }

    class _Cfg:
        class hydrodynamics:  # noqa: N801
            turbulence_length_scale = 1.0
            initial_velocity_guess = 1.0
            turbulence_model = "auto"

        class initialization:  # noqa: N801
            prewet_depth = None

        class mesh:  # noqa: N801
            channel_size = 1.0
            floodplain_size = 1.5

    levels = vc.layer_levels(_Cfg(), data)
    assert levels == sorted(set(levels))            # ascending, unique
    assert len(levels) >= 3 and all(n >= 3 for n in levels)
    # explicit counts are passed through (sorted)
    assert vc.layer_levels(_Cfg(), data, counts=(9, 3, 5)) == [3, 5, 9]


def test_recommendation_picks_fewest_grid_independent(tmp_path):
    # depth-averaged QoI converging as layers increase (changes shrink below 2%)
    levels = [_level(3, 1.20, 0.80, runtime=40),
              _level(5, 1.05, 0.88, runtime=90),
              _level(9, 1.02, 0.895, runtime=240),
              _level(13, 1.015, 0.898, runtime=520)]
    rep = _build_report(levels, ["WATER DEPTH", "SCALAR VELOCITY"], 0.02,
                        np.zeros((6, 2)), case="inn")
    rec = rep.recommendation()
    assert rec["n_levels"] == 9 and rec["converged"]     # first within-tolerance step
    assert rec["speedup_vs_finest"] > 1.0
    assert "9 levels" in rep.format() and "layer thickness dz" in rep.format()

    out = rep.to_xlsx(tmp_path / "vc.xlsx")
    import openpyxl
    ws = openpyxl.load_workbook(out).active
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "RECOMMENDATION" in text and "9 vertical levels" in text


def test_run_vertical_convergence_with_fake_solver():
    probes = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 0.5]])

    # QoI = exact + C / (n-1)**2  -> converges as layers increase (2nd order in dz)
    def simulate(n_levels, pts, quantities):
        dz = 0.9 / (n_levels - 1)
        depth = 1.0 + 0.4 * dz ** 2
        vel = 0.9 + 0.2 * dz ** 2
        return VerticalLevel(
            n_levels=n_levels, dz=dz, n_nodes3d=1000 * n_levels,
            n_elem3d=1800 * (n_levels - 1), time_step=round(0.6 * dz, 4),
            runtime_s=float(n_levels),
            qoi={"WATER DEPTH": np.full(len(pts), depth),
                 "SCALAR VELOCITY": np.full(len(pts), vel)})

    rep = vc.run_vertical_convergence(
        _DummyCfg(), counts=(3, 5, 9), probes=probes, tolerance=0.02,
        simulate=simulate, results_2d=__file__,   # exists; not read (simulate injected)
    )
    assert [lv.n_levels for lv in rep.levels] == [3, 5, 9]
    assert rep.rel_change["WATER DEPTH"][1] < rep.rel_change["WATER DEPTH"][0]
    assert "Vertical-layer convergence study" in rep.format()


class _DummyCfg:
    """Minimal stand-in: counts + probes + simulate are given, so neither the 2D
    result nor the output dirs are touched."""

    name = "dummy"
    model_dir = "."
    results_slf = "r2d.slf"
