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
from hydromate.flux_convergence import (_gross_in_out, _mean_balance_time,
                                        analyze_flux_convergence,
                                        find_steady_window)


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
        results_slf="r2d.slf",
        hydrodynamics=SimpleNamespace(
            listing_printout_period=period, time_step=dt, n_time_steps=n_steps),
        model_path=lambda name: Path(model_dir) / name,
    )


# a minimal built steady case for the hotstart derivation (write_hotstart_cas
# rewrites the initial-conditions / duration / results lines and keeps the rest)
_STEADY_CAS = """\
TITLE : 'stub steady'
GEOMETRY FILE : geometry.slf
BOUNDARY CONDITIONS FILE : boundaries.cli
RESULTS FILE : r2d.slf
DURATION : 5000.0
PRESCRIBED FLOWRATES : 0.;47.0000
PRESCRIBED ELEVATIONS : 329.2400;0.
PREVIOUS COMPUTATION FILE : initial-conditions.slf
INITIAL TIME SET TO ZERO : YES
&ETA
"""


def _fake_extract(imbalance, model_dir):
    """Return a stand-in for pythomac.extract_fluxes yielding a signed two-boundary
    flux table whose relative imbalance follows *imbalance* (inflow +, outflow -)."""
    q_in = np.full(imbalance.size, 47.0)
    q_out = -q_in * (1.0 - imbalance)            # outflow reported negative

    def fake(model_directory="", cas_name="", plotting=True):
        idx = np.arange(imbalance.size, dtype=float)
        df = pd.DataFrame({"Fluxes Boundary 1": q_in,
                           "Fluxes Boundary 2": q_out}, index=idx)
        # mimic the real extract_fluxes side effects: the fluxes CSV + plot
        df.to_csv(Path(model_directory) / "extracted-fluxes.csv")
        if plotting:
            (Path(model_directory) / "flux-convergence.png").write_bytes(b"")
        return df
    return fake


def test_gross_in_out_aggregates_by_sign_and_drops_dry_rows():
    # three boundaries: two inflow (+), one outflow (-); first row is the dry start
    df = pd.DataFrame({
        "Fluxes Boundary 1": [0.0, 30.0, 31.0],
        "Fluxes Boundary 2": [0.0, 17.0, 16.0],
        "Fluxes Boundary 3": [0.0, -45.0, -47.0],
        "Volumes (m3/s)": [0.0, 1.0, 2.0],         # ignored (no "flux" in name)
    })
    times, gin, gout = _gross_in_out(df)
    assert gin.tolist() == [47.0, 47.0]            # 30+17, 31+16; dry row dropped
    assert gout.tolist() == [45.0, 47.0]
    assert times.tolist() == [1.0, 2.0]            # index rows of the kept printouts


def test_find_steady_window_requires_consecutive_printouts():
    t = np.arange(20, dtype=float) * 10.0
    gin = np.full(20, 2.0)
    gout = np.full(20, 2.0) + 5e-4                  # balanced within 1e-3 throughout
    gout[:8] = 1.5                                  # ...except the first 8 printouts
    # strict window of 10 starts at the first balanced printout (index 8, t=80)
    assert find_steady_window(t, gin, gout, abs_tolerance=1e-3, window=10) == 80.0
    # a lone dip below tolerance does not count as steady
    gout = np.full(20, 2.005)
    gout[10] = 2.0
    assert find_steady_window(t, gin, gout, abs_tolerance=1e-3, window=10) is None


def test_mean_balance_time_averages_out_steady_noise():
    # imbalance oscillates +/-3e-3 around zero: never below 1e-3 per printout,
    # but balanced in the 10-printout mean (the ering-revised situation)
    t = np.arange(40, dtype=float) * 10.0
    gin = np.full(40, 2.0)
    gout = 2.0 + 3e-3 * np.where(np.arange(40) % 2 == 0, 1.0, -1.0)
    assert find_steady_window(t, gin, gout, abs_tolerance=1e-3, window=10) is None
    secs = _mean_balance_time(t, gin, gout, abs_tolerance=1e-3, window=10)
    assert secs == 0.0                              # balanced in the mean from the start


def test_converges_and_recommends_time_steps(tmp_path, monkeypatch):
    calc, getconv = _pythomac()
    # imbalance decays past 1e-6 and stays there from a known printout onward
    eps = 10.0 ** (-(np.arange(60) / 6.0))         # 1e0 .. 1e-9.8
    monkeypatch.setattr(flux_convergence, "_import_pythomac",
                        lambda *_: (_fake_extract(eps, tmp_path), calc, getconv))

    period = 50
    cfg = _stub_cfg(tmp_path, period=period, dt=1.0)
    (tmp_path / "steady2d.cas").write_text(_STEADY_CAS)
    fc = analyze_flux_convergence(cfg, tolerance=1e-6)

    # independently reproduce pythomac's converged index from the same series
    iota = calc(np.full(eps.size, 47.0), 47.0 * (1.0 - eps), cas_timestep=period)
    idx = getconv(iota["Relative imbalance"].to_numpy(), convergence_precision=1e-6)

    assert fc.converged is True
    assert fc.converged_time_steps == int(idx) * period
    # converged time = idx * cas_timestep; the fake's index is 0,1,2,... so the
    # back-calculated cas_timestep is 1.0 s
    assert fc.converged_seconds == float(int(idx))
    assert fc.final_imbalance < 1e-6
    # the same four files pythomac's example produces are written into model_dir
    for p in (fc.fluxes_csv, fc.flux_plot, fc.rate_csv, fc.rate_plot):
        assert p is not None and p.exists(), f"missing convergence output: {p}"
    assert {p.name for p in (fc.fluxes_csv, fc.flux_plot, fc.rate_csv, fc.rate_plot)} == {
        "extracted-fluxes.csv", "flux-convergence.png",
        "convergence-rate.csv", "convergence-rate.png"}

    # the absolute steady window is found strictly and the hotstart case is derived
    assert fc.steady_seconds is not None and fc.steady_strict is True
    assert fc.hotstart_cas is not None and fc.hotstart_cas.exists()
    hot = fc.hotstart_cas.read_text()
    assert "PREVIOUS COMPUTATION FILE : r2d.slf" in hot          # continues the run
    assert "RESULTS FILE : r2d-hotstart.slf" in hot              # no self-clobber
    assert f"DURATION : {np.ceil(fc.steady_seconds)}" in hot     # steady end time
    assert "PRESCRIBED FLOWRATES : 0.;47.0000" in hot            # Q kept alive
    assert "PRESCRIBED ELEVATIONS : 329.2400;0." in hot          # H kept alive


def test_not_converged_when_tolerance_never_met(tmp_path, monkeypatch):
    calc, getconv = _pythomac()
    eps = np.full(40, 1e-3)                          # plateau above any tight tol
    monkeypatch.setattr(flux_convergence, "_import_pythomac",
                        lambda *_: (_fake_extract(eps, tmp_path), calc, getconv))

    fc = analyze_flux_convergence(_stub_cfg(tmp_path), tolerance=1e-6)
    assert fc.converged is False
    assert fc.converged_time_steps is None
    assert fc.converged_seconds is None


def test_filling_phase_falls_back_but_writes_all_four_files(tmp_path, monkeypatch):
    """A still-filling run has a flat imbalance (~1, outflow not yet established), so
    pythomac's logn-based rate is all-NaN and its plot crashes; hydromate falls back
    to a direct imbalance plot and still writes the four convergence files."""
    calc, getconv = _pythomac()
    eps = np.full(20, 1.0)                       # outflow ~0 while the reach fills
    monkeypatch.setattr(flux_convergence, "_import_pythomac",
                        lambda *_: (_fake_extract(eps, tmp_path), calc, getconv))

    fc = analyze_flux_convergence(_stub_cfg(tmp_path), tolerance=1e-4)
    assert fc.converged is False
    for p in (fc.fluxes_csv, fc.flux_plot, fc.rate_csv, fc.rate_plot):
        assert p is not None and p.exists(), f"missing convergence output: {p}"
