"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydromate.logsetup import reset_logging

#: The fake solver fixtures (plan §27): stand-ins that sleep, print real-shaped output
#: and return a configurable exit code, so the runner can be tested without TELEMAC or
#: OpenFOAM installed.
FAKES = Path(__file__).parent / "fakes"


@pytest.fixture(autouse=True)
def _reset_hydromate_logging():
    """Drop any logging handlers a test (or pipeline.run) attached, so per-test
    logfiles in tmp dirs don't leak handlers into later tests."""
    yield
    reset_logging()


@pytest.fixture(autouse=True)
def _isolate_hydromate_dirs(tmp_path_factory, monkeypatch):
    """Point every hydromate user directory at a temporary one.

    Without this the suite writes jobs, a SQLite index and solver profiles into the
    developer's real ``~/.config`` and ``~/.local/share`` - and, worse, a test could
    read or cancel one of their actual jobs. Autouse rather than opt-in, because the
    failure mode is silent and only shows up on someone's own machine.
    """
    root = tmp_path_factory.mktemp("hydromate-home")
    for var, name in (("HYDROMATE_CONFIG_DIR", "config"),
                      ("HYDROMATE_DATA_DIR", "data"),
                      ("HYDROMATE_STATE_DIR", "state"),
                      ("HYDROMATE_CACHE_DIR", "cache"),
                      ("HYDROMATE_JOB_ROOT", "jobs")):
        monkeypatch.setenv(var, str(root / name))
    # A launcher preference leaking in from the developer's shell would make the
    # selection tests assert about their machine rather than about the code.
    monkeypatch.delenv("HYDROMATE_LAUNCHER", raising=False)
    yield root


@pytest.fixture
def fake_pysource() -> Path:
    """A TELEMAC setup script that really can be captured and validated."""
    return FAKES / "fake_pysource.sh"


@pytest.fixture
def fake_bashrc() -> Path:
    """The OpenFOAM counterpart."""
    return FAKES / "fake_bashrc.sh"


@pytest.fixture
def fake_case(tmp_path, fake_pysource):
    """A minimal, loadable case wired to the fake TELEMAC, with a built ``.cas``.

    Enough for the job system: the geodata files are never opened, because no test here
    meshes anything - the point is the runner, not the mesher.
    """
    from hydromate.config import load_config

    # Present but empty: nothing here meshes, so the files are never opened - but
    # `hydromate <config> --check` validates that they exist, and that check is part of
    # what these tests cover.
    for name in ("dem.tif", "roi.gpkg", "liquid.gpkg"):
        (tmp_path / name).touch()
    (tmp_path / "case-config.yml").write_text(f"""
project:
  name: fakecase
  crs_epsg: 25832
telemac:
  pysource: {fake_pysource}
  n_processors: 1
geodata:
  dem_initial: dem.tif
  boundary: roi.gpkg
boundaries:
  liquid_boundaries: liquid.gpkg
  prescribed_flowrate: 2.4
  prescribed_elevation: 100.0
hydrodynamics:
  duration: 125
""", encoding="utf-8")
    cfg = load_config(tmp_path / "case-config.yml")
    cfg.ensure_dirs()
    cfg.model_path(cfg.cas_file).write_text("/ fake steering file\n", encoding="utf-8")
    return cfg


@pytest.fixture
def fake_env(monkeypatch):
    """Environment variables that make the fake solvers quick and successful."""
    monkeypatch.setenv("FAKE_STEPS", "3")
    monkeypatch.setenv("FAKE_SLEEP", "0")
    monkeypatch.setenv("FAKE_RC", "0")
    yield


def has_systemd_user() -> bool:
    """Whether a systemd *user manager* is reachable - not merely installed."""
    from hydromate.jobs.launchers.systemd import SystemdUserLauncher
    return SystemdUserLauncher.available()


requires_posix = pytest.mark.skipif(os.name == "nt",
                                    reason="POSIX process groups only")
