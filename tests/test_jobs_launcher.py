"""Launcher selection, command construction, and real process-tree cancellation.

Split deliberately in two. The **selection and argv** tests are host-independent, so they
run everywhere and are what cover the Windows and WSL launchers - which have never been
executed against a real install and say so. The **tree-kill** test is real: it starts a
process that starts a grandchild, and asserts that cancelling removes both. That is the
failure plan §9 exists to prevent, and it cannot be tested with a mock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hydromate.core.environment import EnvironmentKind, SolverEnvironment
from hydromate.core.errors import ConfigError
from hydromate.jobs import procs
from hydromate.jobs.launcher import (LaunchHandle, LaunchState, launcher_for,
                                     runner_argv, select_launcher, write_env_file)
from hydromate.jobs.launchers.posix import PosixDetachedLauncher
from hydromate.jobs.launchers.systemd import SystemdUserLauncher, unit_name
from hydromate.jobs.launchers.windows import (CREATE_NEW_PROCESS_GROUP, DETACHED_PROCESS,
                                              JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                                              WindowsJobObjectLauncher, job_object_name)
from hydromate.jobs.launchers.wsl import WslLauncher, _last_int
from hydromate.jobs.paths import JobDir

FAKES = Path(__file__).parent / "fakes"


# ------------------------------------------------------------------------ selection


def test_a_wsl_environment_forces_the_wsl_launcher(monkeypatch):
    """Not a preference: a captured Linux environment cannot start a Windows process,
    so a preference that overrode this would produce a job that cannot run."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/wsl.exe")
    env = SolverEnvironment(kind=EnvironmentKind.WSL, distro="Ubuntu-22.04")
    assert isinstance(select_launcher(env), WslLauncher)
    assert isinstance(select_launcher(env, prefer="posix"), WslLauncher)


def test_windows_picks_the_job_object_launcher(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(WindowsJobObjectLauncher, "available", classmethod(lambda c, e=None: True))
    assert isinstance(select_launcher(SolverEnvironment(kind=EnvironmentKind.WINDOWS)),
                      WindowsJobObjectLauncher)


def test_posix_is_the_fallback_when_systemd_is_unavailable(monkeypatch):
    monkeypatch.setattr(SystemdUserLauncher, "available",
                        classmethod(lambda cls, env=None: False))
    assert isinstance(select_launcher(SolverEnvironment()), PosixDetachedLauncher)


def test_systemd_is_preferred_when_it_is_available(monkeypatch):
    monkeypatch.setattr(SystemdUserLauncher, "available",
                        classmethod(lambda cls, env=None: True))
    assert isinstance(select_launcher(SolverEnvironment()), SystemdUserLauncher)


def test_an_unknown_launcher_name_is_refused_with_the_alternatives(monkeypatch):
    with pytest.raises(ConfigError) as excinfo:
        select_launcher(SolverEnvironment(), prefer="slurm")
    assert "posix" in (excinfo.value.remedy or "")


def test_asking_for_an_unavailable_launcher_says_what_is_available(monkeypatch):
    monkeypatch.setattr(SystemdUserLauncher, "available",
                        classmethod(lambda cls, env=None: False))
    with pytest.raises(ConfigError) as excinfo:
        select_launcher(SolverEnvironment(), prefer="systemd")
    assert "not available" in str(excinfo.value)


def test_systemd_availability_needs_a_user_manager_not_just_the_binary(monkeypatch):
    """The classic false positive: a bare SSH session without lingering has
    systemd-run on PATH and no user manager at all."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert SystemdUserLauncher.available() is False


def test_a_handle_reconstructs_its_own_launcher():
    """By name, not by re-running selection: a job launched under systemd must be
    cancelled through systemd even if selection would now choose something else."""
    handle = LaunchHandle(backend="posix", pid=1)
    assert isinstance(launcher_for(handle), PosixDetachedLauncher)
    with pytest.raises(ConfigError):
        launcher_for(LaunchHandle(backend="martian", pid=1))


def test_a_handle_round_trips_through_status_json():
    handle = LaunchHandle(backend="systemd", unit="hydromate-x.service", pid=42,
                          proc_started=1.5)
    restored = LaunchHandle.from_dict(handle.as_dict())
    assert restored == handle


def test_a_handle_tolerates_unknown_keys_from_a_newer_hydromate():
    restored = LaunchHandle.from_dict({"backend": "posix", "pid": 3, "cgroup": "/x"})
    assert restored.backend == "posix" and restored.pid == 3


# --------------------------------------------------------------------------- argv


def test_the_runner_argv_falls_back_to_module_form(monkeypatch):
    """Inside a systemd unit the PATH is the solver's, not the user's shell's, so the
    console script is routinely absent."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    argv = runner_argv("/jobs/x")
    assert argv[:3] == [sys.executable, "-m", "hydromate"]
    assert argv[3:] == ["execute", "/jobs/x"]


def test_the_env_file_drops_multiline_values_rather_than_mangling_them(tmp_path, caplog):
    """A truncated PATH produces a job that fails much later with an inexplicable
    'command not found', so the drop has to be visible."""
    import logging
    with caplog.at_level(logging.WARNING, logger="hydromate.jobs.launcher"):
        target = write_env_file(tmp_path / "launch.env",
                                {"GOOD": "/usr/bin", "BAD": "line1\nline2"})
    text = target.read_text(encoding="utf-8")
    assert "GOOD=/usr/bin" in text
    assert "BAD" not in text
    assert "spans lines" in caplog.text and "BAD" in caplog.text


def test_the_systemd_unit_name_is_deterministic():
    """It has to be, or a later process could not find the unit."""
    assert unit_name("2026-08-14-x-steady-aaaaaa") == \
        "hydromate-2026-08-14-x-steady-aaaaaa.service"


def test_the_windows_job_object_is_named():
    """An unnamed Job Object is reachable only through the handle that created it, and
    that handle dies with the submitter - so cancellation from a fresh process would be
    impossible, which is the very failure the Job Object exists to prevent."""
    assert job_object_name("2026-08-14-x-steady-aaaaaa") == \
        "Local\\hydromate-2026-08-14-x-steady-aaaaaa"


def test_the_windows_creation_flags_detach_without_killing_on_close():
    assert CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert DETACHED_PROCESS == 0x00000008
    flags = 0
    flags &= ~JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert not flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_the_wsl_payload_detaches_inside_the_distro_and_returns_the_linux_pid():
    payload = WslLauncher().launch_payload(["hydromate", "execute", "/jobs/x"],
                                           "/jobs/x", "/jobs/x/solver/stdout.log")
    assert "setsid nohup" in payload
    assert "< /dev/null" in payload
    assert payload.rstrip().endswith("echo $!")     # the Linux pid, the only usable handle


def test_the_wsl_command_targets_the_named_distro():
    argv = WslLauncher()._bash("Ubuntu-22.04", "true")
    assert "-d" in argv and "Ubuntu-22.04" in argv
    assert argv[-2:] == ["-lc", "true"]


def test_the_wsl_pid_is_read_from_the_last_line():
    """A login shell may print a banner, and WSL itself sometimes emits a notice."""
    assert _last_int("Welcome to Ubuntu\nsome notice\n4242\n") == 4242
    assert _last_int("no digits here") is None


# --------------------------------------------------------- real detachment and kill


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_detached_job_survives_and_its_whole_tree_can_be_killed(tmp_path):
    """The test that cannot be mocked.

    ``fake_job.py`` spawns a grandchild. Signalling only the process we launched would
    leave that grandchild running - which is exactly what an orphaned ``mpirun`` looks
    like after a naive cancellation.
    """
    jd = JobDir(tmp_path / "2026-08-14-kill-steady-aaaaaa").create()
    marker = tmp_path / "pids.txt"
    launcher = PosixDetachedLauncher()
    handle = launcher.submit(jd, [sys.executable, str(FAKES / "fake_job.py"), str(marker)],
                             env=dict(os.environ), cwd=tmp_path)

    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), "the detached job never started"
    parent, child = (int(x) for x in marker.read_text().split())

    # It is genuinely its own session leader, which is what makes killpg work.
    assert os.getpgid(handle.pid) == handle.pid
    assert launcher.status(handle) is LaunchState.RUNNING
    assert procs.pid_alive(child)

    assert launcher.cancel(handle, grace=5.0)
    for _ in range(60):
        if not procs.pid_alive(parent) and not procs.pid_alive(child):
            break
        time.sleep(0.1)
    assert not procs.pid_alive(parent), "the runner survived cancellation"
    assert not procs.pid_alive(child), "a grandchild was orphaned by cancellation"
    assert launcher.status(handle) is LaunchState.GONE


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_recorded_pid_that_is_gone_reports_gone(tmp_path):
    jd = JobDir(tmp_path / "2026-08-14-gone-steady-aaaaaa").create()
    launcher = PosixDetachedLauncher()
    handle = launcher.submit(jd, [sys.executable, "-c", "pass"], env=dict(os.environ),
                             cwd=tmp_path)
    for _ in range(100):
        if launcher.status(handle) is LaunchState.GONE:
            break
        time.sleep(0.05)
    assert launcher.status(handle) is LaunchState.GONE


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_pid_reuse_is_not_mistaken_for_a_running_job(tmp_path):
    jd = JobDir(tmp_path / "2026-08-14-reuse-steady-aaaaaa").create()
    launcher = PosixDetachedLauncher()
    handle = launcher.submit(jd, [sys.executable, "-c", "import time;time.sleep(30)"],
                             env=dict(os.environ), cwd=tmp_path)
    try:
        assert launcher.status(handle) is LaunchState.RUNNING
        # Same pid, a start time that does not match: a different process wearing the
        # number.
        impostor = LaunchHandle.from_dict({**handle.as_dict(),
                                           "proc_started": (handle.proc_started or 0) + 99})
        assert launcher.status(impostor) is LaunchState.GONE
    finally:
        launcher.cancel(handle, grace=2.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_cancelling_something_already_gone_is_success_not_an_error(tmp_path):
    launcher = PosixDetachedLauncher()
    dead = 4_000_001
    while procs.pid_alive(dead):
        dead += 1
    assert launcher.cancel(LaunchHandle(backend="posix", pid=dead, pgid=dead), grace=1.0)


def test_a_launch_failure_becomes_a_typed_environment_error(tmp_path):
    from hydromate.core.errors import EnvironmentError as HydromateEnvironmentError
    jd = JobDir(tmp_path / "2026-08-14-nope-steady-aaaaaa").create()
    with pytest.raises(HydromateEnvironmentError) as excinfo:
        PosixDetachedLauncher().submit(jd, ["/definitely/not/here"],
                                       env=dict(os.environ), cwd=tmp_path)
    assert excinfo.value.code == "hydromate.environment"
    assert "PATH" in (excinfo.value.remedy or "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_a_detached_job_has_no_controlling_terminal(tmp_path):
    """Inheriting a tty means SIGHUP kills the job when the terminal closes - the exact
    failure this launcher exists to prevent."""
    jd = JobDir(tmp_path / "2026-08-14-tty-steady-aaaaaa").create()
    out = tmp_path / "out.txt"
    handle = PosixDetachedLauncher().submit(
        jd, [sys.executable, "-c",
             f"import sys,os;open({str(out)!r},'w').write(str(os.isatty(0)))"],
        env=dict(os.environ), cwd=tmp_path)
    for _ in range(100):
        if out.exists():
            break
        time.sleep(0.05)
    assert out.read_text() == "False"
    assert handle.backend == "posix"


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_the_fake_solver_prints_a_listing_the_real_parser_understands(tmp_path):
    """If it did not, every progress assertion elsewhere would be self-fulfilling."""
    from hydromate.progress import SolverProgress

    proc = subprocess.run([sys.executable, str(FAKES / "bin" / "telemac2d.py"),
                           str(tmp_path / "steady2d.cas"), "--ncsize=2"],
                          capture_output=True, text=True,
                          env={**os.environ, "FAKE_STEPS": "4", "FAKE_SLEEP": "0"})
    assert proc.returncode == 0
    progress = SolverProgress(100.0, echo=False)
    for line in proc.stdout.splitlines():
        progress.feed(line)
    assert progress.iteration == 400
    assert progress.time == pytest.approx(100.0)
