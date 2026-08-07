"""How the HydroBayesCal driver is located and staged (:mod:`hydromate.bayescal`).

HydroBayesCal is a pip dependency, so the driver comes from the **installed
package** - there is no checkout directory to configure and no second conda
environment to name. These tests pin that, plus the one behavioural patch hydromate
applies to the stock driver.

The tests that need the real package are skipped when it is not installed
(``pip install 'hydromate[calibration]'``), since it is an optional extra.

Run via: mamba run -n hydromate-env pytest tests/test_bayescal_staging.py
"""

from __future__ import annotations

import sys

import pytest

hbc = pytest.importorskip(
    "hydroBayesCal", reason="optional extra: pip install 'hydromate[calibration]'")


def test_driver_comes_from_the_installed_package(tmp_path):
    from hydromate.bayescal import stage_driver

    staged = stage_driver(tmp_path, "bal_telemac.py")

    assert staged.is_file()
    assert staged.parent == tmp_path
    # no environment variable had to be set for this to resolve
    import os
    assert "HYDROBAYESCAL_DIR" not in os.environ


def test_extraction_window_is_patched_to_the_converged_frame(tmp_path):
    """The stock driver averages the last window of frames; on a run marching to
    steady state that averages the residual transient into the calibration values."""
    from hydromate.bayescal import stage_driver

    original = (hbc.driver_path("bal_telemac.py")).read_text()
    staged = stage_driver(tmp_path, "bal_telemac.py").read_text()

    assert original.count('output_extraction_time="mean_last"') > 0   # stock behaviour
    assert 'output_extraction_time="mean_last"' not in staged
    assert staged.count('output_extraction_time="last"') == \
        original.count('output_extraction_time="mean_last"')


def test_multiflow_driver_brings_the_sibling_it_imports(tmp_path):
    """bal_telemac_multiflow.py imports bal_telemac.py by file name, so staging one
    without the other yields an ImportError only at run time, hours in."""
    from hydromate.bayescal import stage_driver

    stage_driver(tmp_path, "bal_telemac_multiflow.py")

    assert (tmp_path / "bal_telemac_multiflow.py").is_file()
    assert (tmp_path / "bal_telemac.py").is_file()


def test_a_checkout_can_still_override_the_installed_package(tmp_path):
    """For developing against an unreleased driver."""
    from hydromate.bayescal import stage_driver

    checkout = tmp_path / "checkout" / "src" / "hydroBayesCal" / "drivers"
    checkout.mkdir(parents=True)
    (checkout / "bal_telemac.py").write_text("# a local edit\n")

    staged = stage_driver(tmp_path / "out", "bal_telemac.py",
                          checkout=tmp_path / "checkout")
    assert staged.read_text() == "# a local edit\n"


def test_missing_driver_names_what_is_available(tmp_path):
    from hydromate.bayescal import stage_driver

    with pytest.raises(FileNotFoundError, match="available:"):
        stage_driver(tmp_path, "bal_nonexistent.py")


def test_launch_uses_this_interpreter_and_no_conda_env(tmp_path, monkeypatch, capsys):
    """The calibration runs in hydromate's own environment - naming a conda env was
    the thing that made this fragile across machines."""
    from types import SimpleNamespace

    from hydromate import bayescal

    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bayescal.subprocess, "run", fake_run)
    cfg = SimpleNamespace(telemac=SimpleNamespace(pysource=tmp_path / "pysource.sh"))
    driver = tmp_path / "bal_telemac.py"
    driver.write_text("")

    bayescal.launch(cfg, driver, tmp_path / "config_Telemac.py")

    command = seen["args"][-1]
    assert sys.executable in command
    assert "mamba run" not in command and "conda run" not in command
    assert "source" in command          # TELEMAC still has to be sourced


def test_launch_reports_an_ignored_env_argument(tmp_path, monkeypatch, capsys):
    """Old call sites passed env='wrr-proj'; say plainly that it no longer applies
    rather than silently ignoring it."""
    from types import SimpleNamespace

    from hydromate import bayescal

    monkeypatch.setattr(bayescal.subprocess, "run",
                        lambda args, **kw: SimpleNamespace(returncode=0))
    cfg = SimpleNamespace(telemac=SimpleNamespace(pysource=tmp_path / "pysource.sh"))
    driver = tmp_path / "bal_telemac.py"
    driver.write_text("")

    bayescal.launch(cfg, driver, tmp_path / "cfg.py", env="wrr-proj")

    assert "ignored" in capsys.readouterr().out
