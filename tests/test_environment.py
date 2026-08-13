"""Entering a solver environment on Linux, Windows or WSL
(:mod:`hydromate.core.environment`).

hydromate used to hardcode ``bash -lc 'source X; cmd'`` - three Linux assumptions in
one line, and the sole reason it could not run on Windows. These tests pin the three
kinds, the safety of the argv construction, and the two things that turned out to be
wrong when the mechanism was first run against the real installations on this machine:

* the TELEMAC sentinel. ``SYSTELCFG`` is *not* exported by the v9.1.1 pysource, so
  requiring it rejected a working install. Sentinels come from inspecting real
  installations, not from documentation, and this test records that.
* setup-script chatter. TELEMAC's pysource prints a ``[WARN] SALOME ...`` line and a
  banner before anything else, and without a delimiter the first captured "variable"
  is that chatter - with a stray ``=`` able to shadow a real entry.

The argv tests run on any host; the capture tests use a stub script, so nothing here
needs a solver. The two tests that touch the real TELEMAC/OpenFOAM installs skip when
they are absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydromate.core.environment import (
    EnvironmentKind, EnvStatus, SolverEnvironment, default_mpi_launcher,
    windows_to_wsl, wsl_to_windows,
)

TELEMAC_PYSOURCE = Path(
    "/home/modelling/telemac-v911/telemac-mascaret/configs/pysource.debian12.sh")
OPENFOAM_BASHRC = Path("/home/modelling/OpenFOAM/OpenFOAM-9/etc/bashrc")


# --------------------------------------------------------------------------- #
# argv construction, per kind
# --------------------------------------------------------------------------- #


def test_posix_sources_the_script_in_bash():
    env = SolverEnvironment(kind="posix", setup_script="/opt/telemac/pysource.sh")
    argv = env.command("telemac2d.py case.cas")
    assert argv[0] == "bash" and argv[1] == "-lc"
    assert "source /opt/telemac/pysource.sh" in argv[2]
    assert argv[2].startswith("set -e;")      # a failing source must abort


def test_windows_calls_a_batch_file_through_cmd():
    env = SolverEnvironment(kind="windows",
                            setup_script=r"C:\telemac\configs\pysource.bat")
    argv = env.command("telemac2d.py case.cas")
    assert argv[:2] == ["cmd", "/c"]
    assert r'call "C:\telemac\configs\pysource.bat" &&' in argv[2]
    assert "bash" not in argv[0]              # `source` is not a thing on cmd


def test_windows_with_a_bundled_bash_uses_that_bash():
    """blueCFD-Core and the ESI package ship their own MSYS bash; entering their
    environment means using *that* one, not cmd and not the system bash."""
    env = SolverEnvironment(kind="windows", shell=r"C:\blueCFD\msys64\usr\bin\bash.exe",
                            setup_script="/opt/blueCFD/OpenFOAM-8/etc/bashrc")
    argv = env.command("interFoam")
    assert argv[0] == r"C:\blueCFD\msys64\usr\bin\bash.exe"
    assert argv[1] == "-lc" and "source" in argv[2]


def test_wsl_wraps_bash_in_the_distro():
    env = SolverEnvironment(kind="wsl", distro="Ubuntu-22.04",
                            setup_script="/opt/openfoam9/etc/bashrc")
    argv = env.command("interFoam -parallel")
    assert argv[:4] == ["wsl.exe", "-d", "Ubuntu-22.04", "--"]
    assert argv[4] == "bash" and "source /opt/openfoam9/etc/bashrc" in argv[6]


def test_wsl_without_a_distro_uses_the_default_one():
    env = SolverEnvironment(kind="wsl", setup_script="/opt/of/etc/bashrc")
    assert env.command("x")[:2] == ["wsl.exe", "--"]


def test_no_setup_script_still_produces_a_runnable_command():
    env = SolverEnvironment(kind="posix")
    assert env.command("echo hi") == ["bash", "-lc", "echo hi"]


def test_the_argv_is_a_list_so_paths_cannot_be_reinterpreted():
    """A path with a space or a quote must not become two arguments, or worse."""
    env = SolverEnvironment(kind="posix",
                            setup_script="/opt/my telemac/pysource.sh")
    payload = env.command("telemac2d.py x.cas")[2]
    assert "'/opt/my telemac/pysource.sh'" in payload      # quoted, single token


# --------------------------------------------------------------------------- #
# MPI launcher
# --------------------------------------------------------------------------- #


def test_mpi_launcher_defaults_per_platform():
    """MS-MPI provides mpiexec; mpirun is not universal."""
    assert default_mpi_launcher(EnvironmentKind.WINDOWS) == "mpiexec"
    assert default_mpi_launcher(EnvironmentKind.POSIX) == "mpirun"
    assert default_mpi_launcher(EnvironmentKind.WSL) == "mpirun"


def test_mpi_command_prefixes_only_when_parallel():
    env = SolverEnvironment(kind="posix")
    assert env.mpi_command("interFoam -parallel", 16) == "mpirun -np 16 interFoam -parallel"
    assert env.mpi_command("interFoam", 1) == "interFoam"
    assert env.mpi_command("interFoam", 0) == "interFoam"


def test_an_explicit_mpi_launcher_wins():
    env = SolverEnvironment(kind="posix", mpi_launcher="srun")
    assert env.mpi_command("x", 4).startswith("srun -np 4")


# --------------------------------------------------------------------------- #
# WSL path translation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("win,posix", [
    (r"C:\scratch\hydromate\case", "/mnt/c/scratch/hydromate/case"),
    (r"D:\data", "/mnt/d/data"),
])
def test_windows_paths_translate_across_the_wsl_boundary(win, posix):
    assert windows_to_wsl(win) == posix
    assert wsl_to_windows(posix).lower().replace("\\", "/").rstrip("/") \
        == win.lower().replace("\\", "/").rstrip("/")


def test_a_bare_drive_translates_both_ways():
    assert windows_to_wsl("C:\\") == "/mnt/c"
    assert wsl_to_windows("/mnt/c") == "C:\\"


def test_translation_is_idempotent_on_an_already_posix_path():
    """It is called unconditionally at every boundary, so it must not corrupt a path
    that has already crossed."""
    assert windows_to_wsl("/mnt/c/scratch") == "/mnt/c/scratch"
    assert windows_to_wsl("/opt/openfoam9/etc/bashrc") == "/opt/openfoam9/etc/bashrc"


def test_only_the_wsl_kind_translates():
    win = r"C:\scratch\case"
    assert SolverEnvironment(kind="wsl").translate(win) == "/mnt/c/scratch/case"
    assert SolverEnvironment(kind="windows").translate(win) == win
    assert SolverEnvironment(kind="posix").translate("/a/b") == "/a/b"


def test_a_non_mnt_posix_path_survives_the_inverse():
    assert wsl_to_windows("/opt/openfoam9") == "/opt/openfoam9"


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_capture_reads_variables_the_script_exports(tmp_path):
    script = tmp_path / "setup.sh"
    script.write_text("export HYDROMATE_TEST=works\nexport SECOND=2\n")
    captured = SolverEnvironment(kind="posix", setup_script=script).capture()
    assert captured["HYDROMATE_TEST"] == "works"
    assert captured["SECOND"] == "2"
    assert "PATH" in captured                 # the whole environment, not just the diff


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_script_chatter_never_becomes_a_variable(tmp_path):
    """The bug this found on a real install: TELEMAC's pysource prints a warning and a
    banner before exporting anything, and without a delimiter the first captured
    'variable' was that chatter - with a stray '=' able to shadow a real entry."""
    script = tmp_path / "noisy.sh"
    script.write_text(
        "echo '[WARN] SALOME at /opt/salome: removed its MED from LD_LIBRARY_PATH'\n"
        "echo 'TELEMAC set: HOMETEL = /opt/telemac'\n"
        "echo 'PATH=/evil/injected'\n"        # would shadow PATH without the marker
        "export HOMETEL=/opt/telemac\n")
    captured = SolverEnvironment(kind="posix", setup_script=script).capture()
    assert captured["HOMETEL"] == "/opt/telemac"
    assert not captured["PATH"].startswith("/evil/injected")
    assert not [k for k in captured if "[WARN]" in k or "\n" in k or " " in k]


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_capture_preserves_a_value_containing_a_newline(tmp_path):
    """Why the dump is NUL-delimited rather than line-split."""
    script = tmp_path / "multiline.sh"
    script.write_text("export MULTI='first\nsecond'\n")
    assert SolverEnvironment(kind="posix",
                             setup_script=script).capture()["MULTI"] == "first\nsecond"


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_overrides_are_applied_on_top_of_the_capture(tmp_path):
    script = tmp_path / "setup.sh"
    script.write_text("export A=1\n")
    env = SolverEnvironment(kind="posix", setup_script=script, overrides={"A": "2"})
    assert env.capture()["A"] == "2"


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_a_missing_setup_script_reports_rather_than_raises():
    """A broken installation is an answer, not an exception - the whole point of
    validating a profile when it is configured rather than when a job is submitted."""
    status = SolverEnvironment(kind="posix", setup_script="/nope/missing.sh").validate(
        "telemac")
    assert isinstance(status, EnvStatus) and not status
    assert "not found" in status.detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_a_script_that_enters_nothing_names_the_missing_sentinels(tmp_path):
    """TELEMAC is not installed system-wide here, so an empty script genuinely cannot
    provide it - which is what makes this the honest negative case."""
    script = tmp_path / "empty.sh"
    script.write_text("true\n")
    status = SolverEnvironment(kind="posix", setup_script=script).validate("telemac")
    assert not status
    assert "HOMETEL" in status.detail
    assert status.missing == ("HOMETEL",)


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_a_sentinel_the_shell_supplies_by_itself_is_reported_as_ambient(tmp_path):
    """Present is not the same as provided. A login shell reads /etc/profile.d, so on a
    machine with a system-wide solver the sentinels arrive whether the configured
    script did anything or not - and a detached job inherits no such thing. Simulated
    here with a shell whose rc file supplies the variable."""
    rcfile = tmp_path / "rc.sh"
    rcfile.write_text("export HOMETEL=/from/the/profile\n")
    fake_shell = tmp_path / "loginbash"
    fake_shell.write_text(f"#!/bin/bash\nsource {rcfile}\nexec /bin/bash \"$@\"\n")
    fake_shell.chmod(0o755)

    empty = tmp_path / "empty.sh"
    empty.write_text("true\n")
    status = SolverEnvironment(kind="posix", shell=str(fake_shell),
                               setup_script=empty).validate("telemac")
    assert status                                    # the solver IS reachable
    assert status.ambient == ("HOMETEL",)            # but not because of this script
    assert "ambient environment" in status.detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_validation_without_a_solver_only_checks_that_it_can_be_entered(tmp_path):
    script = tmp_path / "setup.sh"
    script.write_text("export ANYTHING=1\n")
    assert SolverEnvironment(kind="posix", setup_script=script).validate()


# --------------------------------------------------------------------------- #
# the real installations on this machine
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not TELEMAC_PYSOURCE.is_file(), reason="no TELEMAC install here")
def test_the_real_telemac_environment_validates():
    """Sentinels are chosen from real installs: this pysource exports HOMETEL but not
    SYSTELCFG, and requiring the latter rejected a working TELEMAC."""
    status = SolverEnvironment(kind="posix",
                               setup_script=TELEMAC_PYSOURCE).validate("telemac")
    assert status, status.detail
    assert status.variables["HOMETEL"].endswith("telemac-mascaret")
    assert any("telemac" in p.lower() for p in status.variables["PATH"].split(":"))
    assert not [k for k in status.variables if "\n" in k or k.startswith("[")]


@pytest.mark.skipif(not OPENFOAM_BASHRC.is_file(), reason="no OpenFOAM install here")
def test_the_real_openfoam_environment_validates():
    status = SolverEnvironment(kind="posix",
                               setup_script=OPENFOAM_BASHRC).validate("openfoam")
    assert status, status.detail
    assert status.variables["WM_PROJECT_VERSION"] == "9"
    assert "OpenFOAM 9" in status.detail


# --------------------------------------------------------------------------- #
# config compatibility
# --------------------------------------------------------------------------- #


def test_a_config_predating_environments_still_resolves():
    """Every case written before this existed carries only telemac.pysource; it must
    keep working without migration."""
    env = SolverEnvironment.from_config(None, legacy_script="/opt/telemac/pysource.sh")
    assert env.kind is EnvironmentKind.POSIX
    assert str(env.setup_script) == "/opt/telemac/pysource.sh"


def test_an_explicit_block_wins_over_the_legacy_script():
    env = SolverEnvironment.from_config(
        {"environment": "wsl", "distro": "Ubuntu-22.04",
         "setup_script": "/opt/openfoam9/etc/bashrc"},
        legacy_script="/ignored/pysource.sh")
    assert env.kind is EnvironmentKind.WSL and env.distro == "Ubuntu-22.04"
    assert str(env.setup_script) == "/opt/openfoam9/etc/bashrc"


def test_shell_runtime_still_wraps_the_way_it_always_did():
    """The refactor must not change what an existing TELEMAC case actually runs."""
    from hydromate.env import ShellRuntime

    runtime = ShellRuntime("/opt/telemac/pysource.sh")
    argv = runtime._wrap("telemac2d.py case.cas")
    assert argv[0] == "bash" and argv[1] == "-lc"
    assert "source /opt/telemac/pysource.sh" in argv[2]
    assert runtime.env_script == Path("/opt/telemac/pysource.sh")
