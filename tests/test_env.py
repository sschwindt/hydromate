"""How the TELEMAC solver is launched (:mod:`hydromate.env`).

The launcher scripts (``telemac2d.py`` / ``telemac3d.py``) import numpy, which the
bare pysource shell does not necessarily provide, so they have to be run with an
interpreter that has it. These tests pin *how* that interpreter is chosen, because
the tempting shortcut - wrapping the call in ``mamba run -n <some-env>`` - bakes a
machine assumption into the package (that mamba exists, that the env has that name,
and that ``MAMBA_ROOT_PREFIX`` resolves) and breaks on any machine where one of
those is different.

Run via: mamba run -n hydromate-env pytest tests/test_env.py
"""

from __future__ import annotations

import sys

import pytest


def _runtime(tmp_path, **kw):
    from hydromate.config import TelemacEnv
    from hydromate.env import TelemacRuntime

    pysource = tmp_path / "pysource.sh"
    pysource.write_text("true\n")
    return TelemacRuntime(TelemacEnv(pysource=pysource, **kw))


def _captured_command(runtime, tmp_path, monkeypatch, **kw):
    """The shell command run_solver would run, without running it."""
    seen = {}

    def fake_run(command, cwd=None, check=True, on_line=None):
        seen["command"] = command
        return None

    monkeypatch.setattr(runtime, "run", fake_run)
    runtime.run_solver("case.cas", cwd=tmp_path, **kw)
    return seen["command"]


def test_launcher_runs_under_hydromates_own_interpreter(tmp_path, monkeypatch):
    """Default: the interpreter hydromate is running in - which has numpy by
    construction, since hydromate depends on it."""
    rt = _runtime(tmp_path)
    cmd = _captured_command(rt, tmp_path, monkeypatch, ncsize=1)

    assert sys.executable in cmd
    # the launcher is resolved inside the sourced shell, so no install layout is
    # assumed here either
    assert '"$(command -v telemac2d.py)"' in cmd
    assert "case.cas" in cmd


def test_no_conda_environment_is_hard_coded(tmp_path, monkeypatch):
    """The machine assumption this module must not make."""
    rt = _runtime(tmp_path)
    cmd = _captured_command(rt, tmp_path, monkeypatch, ncsize=1)

    assert "mamba" not in cmd and "conda" not in cmd
    assert "hydromate-env" not in cmd


def test_solver_python_can_be_overridden(tmp_path, monkeypatch):
    """Config first, then the env var, then sys.executable - so a machine can
    redirect it without editing a config, and a case can pin it without touching
    the machine."""
    rt = _runtime(tmp_path, solver_python="/opt/other/bin/python")
    cmd = _captured_command(rt, tmp_path, monkeypatch, ncsize=1)
    assert "/opt/other/bin/python" in cmd

    rt2 = _runtime(tmp_path)
    monkeypatch.setenv("HYDROMATE_TELEMAC_PYTHON", "/opt/env/bin/python")
    cmd2 = _captured_command(rt2, tmp_path, monkeypatch, ncsize=1)
    assert "/opt/env/bin/python" in cmd2

    # the config wins over the environment
    rt3 = _runtime(tmp_path, solver_python="/opt/cfg/bin/python")
    cmd3 = _captured_command(rt3, tmp_path, monkeypatch, ncsize=1)
    assert "/opt/cfg/bin/python" in cmd3


@pytest.mark.parametrize("ncsize,expect", [(1, False), (8, True)])
def test_parallel_flag_follows_the_core_count(tmp_path, monkeypatch, ncsize, expect):
    rt = _runtime(tmp_path)
    cmd = _captured_command(rt, tmp_path, monkeypatch, ncsize=ncsize)
    assert ("--ncsize=" in cmd) is expect
    # the sortie listing the flux analysis reads is always requested, unzipped so a
    # parallel run's copy can be found
    assert "-s --nozip" in cmd


def test_solver_can_be_switched_to_3d(tmp_path, monkeypatch):
    rt = _runtime(tmp_path)
    cmd = _captured_command(rt, tmp_path, monkeypatch, ncsize=1, solver="telemac3d")
    assert '"$(command -v telemac3d.py)"' in cmd
