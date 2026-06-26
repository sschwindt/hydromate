"""Flux / mass-balance (hotstart) convergence analysis.

The novel hydromate-side logic is (a) aggregating Telemac's signed per-boundary
fluxes into gross inflow/outflow, and (b) turning pythomac's converged printout
index into a ``NUMBER OF TIME STEPS`` recommendation. Those are exercised here with
a synthetic converging flux table fed in place of the ``.sortie`` parser, while the
convergence maths come from the real pythomac functions (the package the feature
delegates to). pythomac is the user's local checkout; the tests skip if it is not
importable.

Run via:
    mamba run -n hydromate-env pytest tests/test_flux_convergence.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hydromate import flux_convergence
from hydromate.flux_convergence import _gross_in_out, analyze_flux_convergence


def _pythomac():
    """Import pythomac's real convergence helpers, or skip the test."""
    try:
        from pythomac import calculate_convergence, get_convergence_time  # noqa: F401
    except ModuleNotFoundError:
        candidate = Path(os.environ.get("PYTHOMAC_DIR", "/home/schwindt/github/pythomac"))
        if not (candidate / "pythomac").is_dir():
            pytest.skip("pythomac not importable")
        sys.path.insert(0, str(candidate))
    from pythomac import calculate_convergence, get_convergence_time
    return calculate_convergence, get_convergence_time


def _stub_cfg(model_dir, *, period=50, dt=1.0, n_steps=10000, cas="steady2d.cas"):
    """A minimal duck-typed Config carrying only what the analyzer reads."""
    return SimpleNamespace(
        model_dir=model_dir,
        cas_file=cas,
        hydrodynamics=SimpleNamespace(
            listing_printout_period=period, time_step=dt, n_time_steps=n_steps),
        model_path=lambda name: Path(model_dir) / name,
    )


def _fake_extract(imbalance, model_dir):
    """Return a stand-in for pythomac.extract_fluxes yielding a signed two-boundary
    flux table whose relative imbalance follows *imbalance* (inflow +, outflow -)."""
    q_in = np.full(imbalance.size, 47.0)
    q_out = -q_in * (1.0 - imbalance)            # outflow reported negative

    def fake(model_directory="", cas_name="", plotting=True):
        if plotting:                              # mimic the real flux plot side effect
            (Path(model_directory) / "flux-convergence.png").write_bytes(b"")
        idx = np.arange(imbalance.size, dtype=float)
        return pd.DataFrame({"Fluxes Boundary 1": q_in,
                             "Fluxes Boundary 2": q_out}, index=idx)
    return fake


def test_gross_in_out_aggregates_by_sign_and_drops_dry_rows():
    # three boundaries: two inflow (+), one outflow (-); first row is the dry start
    df = pd.DataFrame({
        "Fluxes Boundary 1": [0.0, 30.0, 31.0],
        "Fluxes Boundary 2": [0.0, 17.0, 16.0],
        "Fluxes Boundary 3": [0.0, -45.0, -47.0],
        "Volumes (m3/s)": [0.0, 1.0, 2.0],         # ignored (no "flux" in name)
    })
    gin, gout = _gross_in_out(df)
    assert gin.tolist() == [47.0, 47.0]            # 30+17, 31+16; dry row dropped
    assert gout.tolist() == [45.0, 47.0]


def test_converges_and_recommends_time_steps(tmp_path, monkeypatch):
    calc, getconv = _pythomac()
    # imbalance decays past 1e-6 and stays there from a known printout onward
    eps = 10.0 ** (-(np.arange(60) / 6.0))         # 1e0 .. 1e-9.8
    monkeypatch.setattr(flux_convergence, "_import_pythomac",
                        lambda *_: (_fake_extract(eps, tmp_path), calc, getconv))

    period = 50
    cfg = _stub_cfg(tmp_path, period=period, dt=1.0)
    fc = analyze_flux_convergence(cfg, tolerance=1e-6)

    # independently reproduce pythomac's converged index from the same series
    iota = calc(np.full(eps.size, 47.0), 47.0 * (1.0 - eps), cas_timestep=period)
    idx = getconv(iota["Relative imbalance"].to_numpy(), convergence_precision=1e-6)

    assert fc.converged is True
    assert fc.converged_time_steps == int(idx) * period
    assert fc.converged_seconds == fc.converged_time_steps * 1.0
    assert fc.final_imbalance < 1e-6
    assert fc.flux_plot is not None and fc.flux_plot.exists()
    assert fc.rate_plot is not None and fc.rate_plot.exists()


def test_not_converged_when_tolerance_never_met(tmp_path, monkeypatch):
    calc, getconv = _pythomac()
    eps = np.full(40, 1e-3)                          # plateau above any tight tol
    monkeypatch.setattr(flux_convergence, "_import_pythomac",
                        lambda *_: (_fake_extract(eps, tmp_path), calc, getconv))

    fc = analyze_flux_convergence(_stub_cfg(tmp_path), tolerance=1e-6)
    assert fc.converged is False
    assert fc.converged_time_steps is None
    assert fc.converged_seconds is None
