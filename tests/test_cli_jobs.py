"""The CLI: job verbs, the JSON envelope, exit codes, and backward compatibility.

Everything goes through ``main(argv)`` in-process rather than through a subprocess, so a
failure points at a line rather than at a return code. The one thing that genuinely needs
a subprocess - that a submitted job outlives the shell that submitted it - is in
``test_jobs_launcher.py``, where it can be asserted on the process table.
"""

from __future__ import annotations

import json


from axqua.cli import main
from axqua.jobs.model import JobKind, JobState
from axqua.jobs.paths import JobDir
from axqua.jobs.store import read_status


def _json(capsys) -> dict:
    """Parse the JSON document, which is always on stdout alone."""
    out = capsys.readouterr().out
    return json.loads(out)


def _make_job(cfg, kind=JobKind.STEADY_RUN, **kwargs) -> JobDir:
    from axqua.jobs import submit as submit_mod
    spec = submit_mod.build_spec(cfg, kind, **kwargs)
    return submit_mod.create_job(cfg, spec)


# ------------------------------------------------------- backward compatibility


def test_the_bare_config_form_still_builds(fake_case, capsys):
    """``axqua <config> --check`` is quoted in the docs and in every case runbook."""
    assert main([str(fake_case.config_dir / "case-config.yml"), "--check"]) == 0


def test_case_status_still_works_under_its_old_spelling(fake_case, capsys):
    config = str(fake_case.config_dir / "case-config.yml")
    assert main(["status", config]) == 0
    assert "telemac" in capsys.readouterr().out


def test_the_old_spelling_says_where_the_new_one_is(fake_case, caplog):
    import logging
    config = str(fake_case.config_dir / "case-config.yml")
    with caplog.at_level(logging.WARNING, logger="axqua"):
        main(["status", config])
    assert "case-status" in caplog.text


def test_the_new_spelling_works_too(fake_case, capsys):
    config = str(fake_case.config_dir / "case-config.yml")
    assert main(["case-status", config]) == 0
    assert "telemac" in capsys.readouterr().out


# ----------------------------------------------------- the status disambiguation


def test_status_of_a_job_id_means_the_job(fake_case, capsys):
    jd = _make_job(fake_case)
    assert main(["status", jd.job_id]) == 0
    assert jd.job_id in capsys.readouterr().out


def test_status_of_a_path_means_the_case(fake_case, capsys):
    """A job id is never a path and a path never matches the job-id grammar, so the
    rule is a rule rather than a guess."""
    config = str(fake_case.config_dir / "case-config.yml")
    main(["status", config])
    out = capsys.readouterr().out
    assert "capabilit" in out or "telemac" in out


def test_status_of_something_that_is_neither_says_so(capsys):
    code = main(["status", "not-a-job-and-not-a-path", "--json"])
    payload = _json(capsys)
    assert code != 0 and payload["ok"] is False
    assert "job id" in payload["error"]["message"] or \
        "job id" in (payload["error"].get("remedy") or "")


# --------------------------------------------------------------- the JSON envelope


def test_every_json_result_uses_one_envelope(fake_case, capsys):
    jd = _make_job(fake_case)
    for argv in (["list", "--json"], ["status", jd.job_id, "--json"]):
        main(argv)
        payload = _json(capsys)
        assert set(payload) == {"ok", "command", "axqua", "data", "error"}
        assert payload["ok"] is True and payload["error"] is None


def test_a_json_failure_carries_the_structured_error(capsys):
    main(["status", "2026-01-01-nothing-steady-aaaaaa", "--json"])
    payload = _json(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"].startswith("axqua.")
    assert payload["error"]["remedy"]


def test_case_status_json_is_the_capability_matrix(fake_case, capsys):
    """This is what the QGIS plugin reads to decide which tabs to show, so its shape
    is a contract."""
    config = str(fake_case.config_dir / "case-config.yml")
    assert main(["case-status", config, "--json"]) == 0
    data = _json(capsys)["data"]
    solvers = {s["solver"]: s for s in data["solvers"]}
    assert {"telemac", "openfoam"} <= set(solvers)

    telemac = solvers["telemac"]
    assert telemac["enabled"] is True
    caps = {c["capability"]: c for c in telemac["capabilities"]}
    assert caps["steady2d"]["implemented"] == "yes"
    # OpenFOAM has no depth-averaged mode: a category error, not a gap - and the plugin
    # hides such a tab rather than showing it disabled.
    assert {c["capability"]: c for c in solvers["openfoam"]["capabilities"]
            }["steady2d"]["implemented"] == "n/a"


def test_narration_never_pollutes_the_json_document(fake_case, capsys):
    """stdout carries the document alone, so a caller can parse it unconditionally."""
    main(["case-status", str(fake_case.config_dir / "case-config.yml"), "--json"])
    captured = capsys.readouterr()
    json.loads(captured.out)          # must parse with nothing prepended


# --------------------------------------------------------------------- exit codes


def test_exit_codes_distinguish_the_error_categories(fake_case, capsys, tmp_path):
    from axqua import jobcli
    # A bad job reference is a config error.
    assert main(["status", "2026-01-01-x-steady-aaaaaa"]) == jobcli.EXIT_CONFIG
    # An unknown kind, likewise.
    assert main(["submit", str(fake_case.config_dir / "case-config.yml"),
                 "--kind", "nonsense"]) == jobcli.EXIT_CONFIG


# --------------------------------------------------------------------------- list


def test_list_reports_no_jobs_rather_than_failing(capsys):
    assert main(["list"]) == 0
    assert "no jobs" in capsys.readouterr().out


def test_list_shows_a_created_job(fake_case, capsys):
    jd = _make_job(fake_case)
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert jd.job_id in out and "telemac" in out


def test_list_filters(fake_case, capsys):
    _make_job(fake_case)
    main(["list", "--state", "COMPLETED", "--json"])
    assert _json(capsys)["data"] == []


def test_list_rebuild_reconstructs_from_the_directories(fake_case, capsys, tmp_path):
    from axqua.jobs.index import JobIndex
    jd = _make_job(fake_case)
    JobIndex().rebuild(jd.root.parent)          # populate, then wipe
    JobIndex()._connect
    main(["list", "--rebuild", "--json"])
    ids = [row["job_id"] for row in _json(capsys)["data"]]
    assert jd.job_id in ids


# ------------------------------------------------------------------ submit / kinds


def test_submit_lists_the_kinds(capsys):
    assert main(["submit", "x", "--help-kinds"]) == 0
    out = capsys.readouterr().out
    assert "steady" in out and "mesh-convergence" in out


def test_submit_needs_a_kind(fake_case, capsys):
    code = main(["submit", str(fake_case.config_dir / "case-config.yml"), "--json"])
    payload = _json(capsys)
    assert code != 0
    assert "--kind" in payload["error"]["message"]
    assert "--help-kinds" in payload["error"]["remedy"]


def test_submit_dry_run_creates_nothing(fake_case, capsys, tmp_path):
    from axqua.jobs import paths
    config = str(fake_case.config_dir / "case-config.yml")
    assert main(["submit", config, "--kind", "steady", "--dry-run", "--json"]) == 0
    spec = _json(capsys)["data"]
    assert spec["kind"] == "steady"
    assert not list(paths.job_root().glob("*")) if paths.job_root().exists() else True


def test_submit_options_are_parsed_as_json_where_possible(fake_case, capsys):
    config = str(fake_case.config_dir / "case-config.yml")
    main(["submit", config, "--kind", "steady", "--dry-run", "--json",
          "--option", "ncsize=8"])
    assert _json(capsys)["data"]["options"]["ncsize"] == 8


def test_an_unknown_option_is_refused_before_anything_is_created(fake_case, capsys):
    config = str(fake_case.config_dir / "case-config.yml")
    code = main(["submit", config, "--kind", "steady", "--dry-run", "--json",
                 "--option", "ncsizee=8"])
    assert code != 0
    assert "ncsizee" in _json(capsys)["error"]["message"]


# ----------------------------------------------------------------- execute / logs


def test_execute_runs_the_job_here_and_now(fake_case, capsys, monkeypatch):
    # Not `tests.test_jobs_executor`: pytest puts the test directory itself on
    # sys.path, not its parent, so the package spelling only works when the
    # repository root happens to be importable too.
    from test_jobs_executor import _FakeSpec, RecordingBackend
    from axqua.core import registry
    from axqua.core.capabilities import Capability, Support
    from axqua.core.registry import CapabilitySpec

    backend = RecordingBackend()
    _FakeSpec.instances["telemac"] = backend
    previous = registry._REGISTRY.get("telemac")
    registry.register(_FakeSpec(name="telemac", title="fake", config_key="telemac",
                               capabilities={c: CapabilitySpec(support=Support.SUPPORTED)
                                             for c in Capability},
                               enabled=lambda cfg: True), replace=True)
    try:
        jd = _make_job(fake_case)
        assert main(["execute", str(jd.root)]) == 0
        assert read_status(jd).state is JobState.COMPLETED
    finally:
        _FakeSpec.instances.pop("telemac", None)
        if previous is not None:
            registry.register(previous, replace=True)


def test_logs_locates_the_runner_log(fake_case, capsys):
    jd = _make_job(fake_case)
    jd.runner_log.write_text("hello from the runner\n", encoding="utf-8")
    assert main(["logs", jd.job_id, "--path"]) == 0
    assert str(jd.runner_log) in capsys.readouterr().out


def test_logs_prints_the_tail(fake_case, capsys):
    jd = _make_job(fake_case)
    jd.runner_log.write_text("\n".join(f"line {i}" for i in range(200)) + "\n",
                             encoding="utf-8")
    main(["logs", jd.job_id, "--lines", "3"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["line 197", "line 198", "line 199"]


def test_logs_says_so_when_there_is_nothing_yet(fake_case, capsys):
    jd = _make_job(fake_case)
    assert main(["logs", jd.job_id, "--json"]) != 0
    assert "no runner log" in _json(capsys)["error"]["message"]


def test_the_solver_listing_is_a_separate_stream(fake_case, capsys):
    """Plan §23: axqua's narration and the solver's own output are different
    artifacts with different rotation policies."""
    jd = _make_job(fake_case)
    jd.runner_log.write_text("runner narration\n", encoding="utf-8")
    (jd.solver_logs / "telemac2d.log").write_text("ITERATION 100\n", encoding="utf-8")
    main(["logs", jd.job_id, "--solver", "--path"])
    assert "telemac2d.log" in capsys.readouterr().out


# --------------------------------------------------------------------- cancel


def test_cancelling_a_job_that_never_launched_settles_it(fake_case, capsys):
    jd = _make_job(fake_case)
    assert main(["cancel", jd.job_id, "--timeout", "1"]) == 0
    assert read_status(jd).state is JobState.CANCELLED


def test_cancelling_a_finished_job_changes_nothing(fake_case, capsys):
    jd = _make_job(fake_case)
    status = read_status(jd)
    status.transition(JobState.STARTING)
    status.transition(JobState.RUNNING)
    status.transition(JobState.COMPLETED)
    from axqua.jobs.store import write_status
    write_status(jd, status)

    assert main(["cancel", jd.job_id, "--json"]) == 0
    assert _json(capsys)["data"]["changed"] is False


# ------------------------------------------------------------------- profiles


def test_profiles_on_a_fresh_install_reports_emptiness(capsys):
    assert main(["profiles"]) == 0
    assert "no profiles" in capsys.readouterr().out


def test_profiles_path_points_into_the_config_dir(capsys):
    assert main(["profiles", "path"]) == 0
    assert "profiles.yml" in capsys.readouterr().out


def test_the_plan_example_profiles_parse_verbatim(tmp_path, monkeypatch, capsys):
    """The four profiles in plan §6, unchanged. If the schema drifted from what the
    plan documents, this is what would notice."""
    monkeypatch.setenv("AXQUA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "profiles.yml").write_text("""
profiles:
  telemac_linux:
    solver: telemac
    environment: posix
    setup_script: /home/user/telemac/v9.1/configs/pysource.sh
    config_name: ubuntu
    mpi_launcher: mpirun
    mpi_processes: 16
    working_root: /scratch/axqua
  telemac_windows:
    solver: telemac
    environment: windows
    setup_script: C:\\telemac\\v9.1\\configs\\pysource.bat
    mpi_launcher: mpiexec
    mpi_processes: 8
    working_root: D:\\scratch\\axqua
  openfoam_linux:
    solver: openfoam
    environment: posix
    setup_script: /opt/openfoam9/etc/bashrc
    mpi_launcher: mpirun
    mpi_processes: 32
    working_root: /scratch/axqua
    postprocess:
      - foamToVTK
  openfoam_windows:
    solver: openfoam
    environment: wsl
    distro: Ubuntu-22.04
    setup_script: /opt/openfoam9/etc/bashrc
    mpi_launcher: mpirun
    mpi_processes: 16
    working_root: /scratch/axqua
""", encoding="utf-8")
    from axqua.jobs.profiles import load_profiles
    profiles = load_profiles()
    assert set(profiles) == {"telemac_linux", "telemac_windows", "openfoam_linux",
                             "openfoam_windows"}
    assert profiles["openfoam_linux"].postprocess == ["foamToVTK"]
    assert profiles["openfoam_windows"].distro == "Ubuntu-22.04"
    # A WSL setup_script is a *Linux* path and must not be resolved against a Windows
    # base, or it would silently become nonsense on the host filesystem.
    assert str(profiles["openfoam_windows"].setup_script) == "/opt/openfoam9/etc/bashrc"
    # TELEMAC's systel config travels as an environment override rather than widening
    # the solver-neutral SolverEnvironment.
    assert profiles["telemac_linux"].solver_environment().overrides["USETELCFG"] == \
        "ubuntu"
    assert main(["profiles", "list"]) == 0


def test_a_profile_derived_from_the_case_config_needs_no_profiles_file(fake_case):
    """Every existing case already carries telemac.pysource, so the profile system is
    an addition rather than a new prerequisite."""
    from axqua.jobs.profiles import from_config
    profile = from_config(fake_case, "telemac")
    assert profile.solver == "telemac"
    assert profile.validate().ok
