"""Plugin tests that need neither QGIS nor a solver.

This covers the parts of the plugin that are pure logic: executable discovery, the JSON
boundary, the project file, the polling policy, and the capability-to-tab mapping. Those
are also the parts most likely to break silently, because a wrong answer there looks like
a working plugin that shows the wrong thing.

The widgets are not tested here - they need a QGIS application object. The
QGIS-in-the-loop checks are in ``test_plugin_qgis.py``, which skips itself when PyQGIS is
absent.

Run with::

    pytest qgis_plugin/tests
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Loaded under an alias by conftest, because the plugin folder and the installed library
# share the name ``axqua`` and only one of them can own it in a pytest process.
from axqua_plugin.core import job_model, project as project_io  # noqa: E402
from axqua_plugin.core import runner_client  # noqa: E402
from axqua_plugin.gui.capability_tabs import CaseView  # noqa: E402


# ------------------------------------------------------------------ metadata


def test_metadata_declares_both_qgis_generations():
    """QGIS 4 is Qt6-only and decides compatibility from ``qgisMaximumVersion``; the old
    ``supportsQt6`` flag was removed from core and is ignored."""
    text = (PLUGIN_ROOT / "axqua" / "metadata.txt").read_text(encoding="utf-8")
    fields = dict(line.split("=", 1) for line in text.splitlines()
                  if "=" in line and not line.startswith(" "))
    assert fields["qgisMinimumVersion"].strip() == "3.44"
    assert float(fields["qgisMaximumVersion"].strip()) >= 4.0
    assert "supportsQt6" not in fields
    # plugins.qgis.org requires all three links plus a GPL-compatible licence.
    for key in ("homepage", "repository", "tracker", "license", "description", "about"):
        assert fields[key].strip(), f"metadata.txt needs a {key}"
    assert "GPL" in fields["license"]


def test_no_compiled_resources_are_shipped():
    """A ``.qrc``-compiled module is built against one Qt major version and fails to
    import on the other - the most common reason a plugin loads on 3.x and not on 4."""
    offenders = [p.name for p in (PLUGIN_ROOT / "axqua").rglob("*")
                 if p.suffix == ".qrc" or p.name.endswith("_rc.py")]
    assert offenders == []


def test_nothing_imports_pyqt5_or_pyqt6_directly():
    """``qgis.PyQt`` is the shim that makes one codebase work on both."""
    import re
    pattern = re.compile(r"^\s*(?:from|import)\s+PyQt[56]\b", re.M)
    offenders = [p.relative_to(PLUGIN_ROOT) for p in (PLUGIN_ROOT / "axqua").rglob("*.py")
                 if pattern.search(p.read_text("utf-8"))]
    assert offenders == []


def test_the_plugin_never_imports_axqua():
    """The rule the whole architecture rests on: QGIS's Python is not the solver's."""
    import re
    # `from axqua...` inside the plugin package refers to the plugin itself, so the
    # test looks for the two spellings that would reach the *installed* package.
    pattern = re.compile(r"^\s*import\s+axqua\s*$|^\s*from\s+axqua\s+import\s+"
                         r"(?!annotations)", re.M)
    offenders = []
    for path in (PLUGIN_ROOT / "axqua").rglob("*.py"):
        text = path.read_text("utf-8")
        if pattern.search(text):
            offenders.append(path.relative_to(PLUGIN_ROOT))
    assert offenders == []


# ----------------------------------------------------------- executable discovery


def _fake_axqua(tmp_path: Path, *, version: str = "axqua 0.2.0",
                    exit_code: int = 0) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "axqua"
    script.write_text(f"#!/bin/sh\necho '{version}'\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.skipif(os.name == "nt", reason="shell script stub")
def test_discovery_order_prefers_the_setting(tmp_path, monkeypatch):
    configured = _fake_axqua(tmp_path / "a", version="axqua 9.9.9")
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    other = _fake_axqua(tmp_path / "b", version="axqua 0.0.1")
    monkeypatch.setenv(runner_client.ENV_VAR, str(other))

    info = runner_client.find_executable(str(configured))
    assert info.source == "settings" and info.version == "9.9.9"


@pytest.mark.skipif(os.name == "nt", reason="shell script stub")
def test_discovery_falls_back_to_the_environment_then_path(tmp_path, monkeypatch):
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    from_env = _fake_axqua(tmp_path / "b")
    monkeypatch.setenv(runner_client.ENV_VAR, str(from_env))
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert runner_client.find_executable(None).source == "environment"

    monkeypatch.delenv(runner_client.ENV_VAR, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: str(from_env))
    assert runner_client.find_executable(None).source == "PATH"


def test_a_missing_executable_names_every_place_it_looked(monkeypatch):
    """Never a silent fallback to a different interpreter (plan §28)."""
    monkeypatch.delenv(runner_client.ENV_VAR, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(runner_client.RunnerNotFound) as excinfo:
        runner_client.find_executable(None)
    message = str(excinfo.value)
    assert "plugin setting" in message
    assert runner_client.ENV_VAR in message
    assert "PATH" in message
    # The instruction has to be one that actually works: aXqua is not on PyPI yet,
    # and guidance that 404s is worse than none.
    assert "github.com/sschwindt/aXqua" in message


@pytest.mark.skipif(os.name == "nt", reason="shell script stub")
def test_something_that_is_not_axqua_is_rejected(tmp_path, monkeypatch):
    """A wrong binary must be caught at configuration time, not at submit time."""
    impostor = _fake_axqua(tmp_path, version="Python 3.12.1")
    monkeypatch.delenv(runner_client.ENV_VAR, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(runner_client.RunnerNotFound) as excinfo:
        runner_client.find_executable(str(impostor))
    assert "does not look like axqua" in str(excinfo.value)


# ------------------------------------------------------------------ the envelope


class _StubClient(runner_client.RunnerClient):
    """A client whose subprocess call is replaced by a canned document."""

    def __init__(self, payload: dict, *, returncode: int = 0) -> None:
        super().__init__("stub")
        self._payload = payload
        self._returncode = returncode

    @property
    def info(self):
        return runner_client.RunnerInfo("stub", "0.2.0", "settings")

    def call(self, args, *, timeout=None, expect_json=True):
        result = runner_client.Result(
            ok=bool(self._payload.get("ok")),
            command=str(self._payload.get("command") or ""),
            data=self._payload.get("data"),
            error=self._payload.get("error") or {},
            returncode=self._returncode)
        if not result.ok:
            error = result.error
            raise runner_client.RunnerError(
                str(error.get("message")), code=str(error.get("code") or ""),
                remedy=str(error.get("remedy") or ""))
        return result


def test_a_structured_failure_reaches_the_user_with_its_remedy():
    client = _StubClient({"ok": False, "error": {
        "code": "axqua.environment",
        "message": "the telemac environment is not available",
        "remedy": "Check the setup script in the solver profile."}}, returncode=4)
    with pytest.raises(runner_client.RunnerError) as excinfo:
        client.list_jobs()
    assert excinfo.value.code == "axqua.environment"
    assert "setup script" in excinfo.value.user_text()


def test_output_that_is_not_json_is_reported_as_such(tmp_path, monkeypatch):
    script = tmp_path / "axqua"
    script.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo "
                      "'axqua 0.2.0'; else echo 'Segmentation fault'; fi\n",
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    client = runner_client.RunnerClient(str(script))
    with pytest.raises(runner_client.RunnerError) as excinfo:
        client.list_jobs()
    assert "not JSON" in str(excinfo.value)


def test_a_document_preceded_by_stray_output_still_parses():
    """Narration goes to stderr, but a library warning on stdout should degrade the
    message rather than the feature."""
    parsed = runner_client._parse('WARNING: something\n{"ok": true, "data": []}')
    assert parsed == {"ok": True, "data": []}


def test_arguments_are_passed_as_a_list_never_a_shell_string(tmp_path, monkeypatch):
    """A path with a space or a quote must not become two arguments (plan §7)."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        class Proc:
            returncode = 0
            stdout = '{"ok": true, "data": {}}'
            stderr = ""
        return Proc()

    monkeypatch.setattr(runner_client.subprocess, "run", fake_run)
    client = runner_client.RunnerClient("stub")
    client._info = runner_client.RunnerInfo("stub", "0.2.0", "settings")
    client.case_status("/tmp/a case/with 'quotes'/case-config.yml")
    assert isinstance(seen["argv"], list)
    assert "/tmp/a case/with 'quotes'/case-config.yml" in seen["argv"]


# -------------------------------------------------------------------- the project


def test_the_project_file_round_trips(tmp_path):
    project = project_io.AxquaProject(name="inn")
    project.path = tmp_path / "inn.axqua-prj"
    project.add_case(tmp_path / "cases" / "case-config.yml")
    project.profile = "telemac_linux"
    project.job_root = "/scratch/axqua"
    written = project_io.save(project)

    reloaded = project_io.load(written)
    assert reloaded.name == "inn"
    assert reloaded.profile == "telemac_linux"
    assert reloaded.job_root == "/scratch/axqua"
    assert reloaded.active_case_path() == (tmp_path / "cases" / "case-config.yml")


def test_a_case_beside_the_project_is_stored_relatively(tmp_path):
    """So the project directory can be moved or shared without every path breaking."""
    project = project_io.AxquaProject(path=tmp_path / "p.axqua-prj")
    stored = project.add_case(tmp_path / "cases" / "case-config.yml")
    assert stored == str(Path("cases") / "case-config.yml")


def test_a_case_elsewhere_stays_absolute(tmp_path):
    project = project_io.AxquaProject(path=tmp_path / "p.axqua-prj")
    stored = project.add_case("/elsewhere/case-config.yml")
    assert stored == "/elsewhere/case-config.yml"


def test_status_in_a_project_file_is_ignored_not_trusted(tmp_path):
    """Older drafts carried job state. The job directories are authoritative, and a
    stale copy here would show a state that has not been true for days."""
    path = tmp_path / "old.axqua-prj"
    path.write_text(json.dumps({"schema_version": 1, "cases": [],
                                "status": {"job-1": "RUNNING"},
                                "jobs": ["job-1"]}), encoding="utf-8")
    project = project_io.load(path)
    assert not hasattr(project, "status")
    assert project_io.save(project, path) and "status" not in path.read_text()


def test_a_newer_project_file_is_refused_clearly(tmp_path):
    path = tmp_path / "future.axqua-prj"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        project_io.load(path)
    assert "Update the plugin" in str(excinfo.value)


def test_saving_is_atomic(tmp_path):
    project = project_io.AxquaProject(path=tmp_path / "p.axqua-prj", name="p")
    project_io.save(project)
    assert [p.name for p in tmp_path.iterdir()] == ["p.axqua-prj"]


# ---------------------------------------------------------------- polling policy


def _job(state: str, **kwargs) -> job_model.Job:
    return job_model.Job(job_id="2026-08-14-x-steady-aaaaaa", root=Path("/nowhere"),
                         state=state, **kwargs)


def test_a_terminal_job_is_never_polled_again(tmp_path):
    table = job_model.JobTable()
    table.replace([{"job_id": "j1", "root": str(tmp_path), "state": "COMPLETED"}])
    (tmp_path / "status.json").write_text(
        json.dumps({"state": "RUNNING"}), encoding="utf-8")
    assert table.poll() == []


def test_polling_stops_when_nothing_is_running():
    table = job_model.JobTable()
    table.replace([{"job_id": "j1", "root": "/x", "state": "COMPLETED"}])
    assert table.interval_ms(visible=True) == 0


def test_polling_slows_down_when_the_dock_is_hidden():
    table = job_model.JobTable()
    table.replace([{"job_id": "j1", "root": "/x", "state": "RUNNING"}])
    assert table.interval_ms(visible=True) == job_model.INTERVAL_ACTIVE_MS
    assert table.interval_ms(visible=False) == job_model.INTERVAL_HIDDEN_MS


def test_an_unchanged_status_file_is_not_reparsed(tmp_path):
    """Stat before parse - the single biggest saving, because most polls find nothing."""
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"state": "RUNNING", "progress":
                                  {"kind": "solver_run", "iteration": 5}}),
                      encoding="utf-8")
    job = job_model.Job(job_id="j", root=tmp_path, state="RUNNING")
    assert job.refresh_from_disk() is True         # first read
    assert job.refresh_from_disk() is False        # mtime unchanged


def test_a_status_file_caught_mid_replace_is_not_an_error(tmp_path):
    status = tmp_path / "status.json"
    status.write_text("{ truncated", encoding="utf-8")
    job = job_model.Job(job_id="j", root=tmp_path, state="RUNNING")
    assert job.refresh_from_disk() is False        # the next poll will get it
    assert job.state == "RUNNING"


def test_a_refresh_keeps_live_progress_from_being_blanked(tmp_path):
    """``list`` reports state but not the iteration count, so a naive replace would make
    a running job look stalled on every refresh."""
    table = job_model.JobTable()
    table.replace([{"job_id": "j1", "root": str(tmp_path), "state": "RUNNING"}])
    table.get("j1").progress = {"kind": "solver_run", "iteration": 500}
    table.replace([{"job_id": "j1", "root": str(tmp_path), "state": "RUNNING"}])
    assert table.get("j1").progress["iteration"] == 500


@pytest.mark.parametrize("progress,expected", [
    ({"kind": "solver_run", "simulated_time": 50.0, "duration": 100.0, "iteration": 20},
     "50%"),
    ({"kind": "ladder", "levels_done": 2, "levels_total": 4}, "level 2/4"),
    ({"kind": "calibration", "iteration": 12, "max_iterations": 50,
      "best_objective": 0.0472}, "iter 12/50"),
    ({"kind": "openfoam_build", "cells": 1579074}, "1,579,074 cells"),
])
def test_progress_reads_correctly_for_every_job_kind(progress, expected):
    """One column that is always meaningful, which is the visible payoff of typing
    progress per kind rather than flattening it."""
    job = _job("RUNNING", progress=progress)
    assert expected in job.progress_text


# ------------------------------------------------------------- capability -> tabs


MATRIX = {
    "case_dir": "/cases/inn",
    "solvers": [
        {"solver": "telemac", "enabled": True, "env_ok": True, "capabilities": [
            {"capability": "steady2d", "implemented": "yes", "configured": True,
             "built": True, "run": False},
            {"capability": "free_surface_3d", "implemented": "n/a"},
            {"capability": "morphodynamics", "implemented": "yes", "configured": False,
             "built": None, "run": None},
        ]},
        {"solver": "openfoam", "enabled": False, "env_ok": None, "capabilities": [
            {"capability": "steady2d", "implemented": "n/a"},
            {"capability": "morphodynamics", "implemented": "no"},
        ]},
    ],
}


def test_a_category_error_is_hidden_entirely():
    """OpenFOAM's free surface is inherently 3D and transient, so a 'Steady 2D' tab
    under it is not a missing feature - showing it disabled would send the user looking
    for a switch that does not exist."""
    view = CaseView.from_payload(MATRIX)
    telemac = view.solver("telemac")
    names = [c.name for c in telemac.visible_capabilities]
    assert "free_surface_3d" not in names
    assert "steady2d" in names


def test_an_unimplemented_capability_is_shown_disabled_with_a_reason():
    view = CaseView.from_payload(MATRIX)
    capability = view.solver("openfoam").capability("morphodynamics")
    assert capability.visible is True
    assert capability.enabled is False
    assert "gap in axqua" in capability.reason


def test_a_capability_the_case_does_not_ask_for_says_so():
    view = CaseView.from_payload(MATRIX)
    capability = view.solver("telemac").capability("morphodynamics")
    assert capability.enabled is True and capability.can_submit is False
    assert "case-config.yml" in capability.reason


def test_actions_follow_configured_built_and_run():
    view = CaseView.from_payload(MATRIX)
    steady = view.solver("telemac").capability("steady2d")
    assert steady.can_submit is True
    assert steady.needs_build is False        # built is True
    assert steady.has_results is False        # never run
    assert steady.state_text == "configured, built"


def test_only_declared_solvers_get_tabs():
    """A case with no ``openfoam:`` block should not grow an OpenFOAM tab."""
    view = CaseView.from_payload(MATRIX)
    assert [s.name for s in view.enabled_solvers] == ["telemac"]


def test_a_capability_this_plugin_has_never_heard_of_still_gets_a_tab():
    """The point of generating tabs: axqua can grow one without a plugin release."""
    payload = {"solvers": [{"solver": "telemac", "enabled": True, "capabilities": [
        {"capability": "ice_jam", "implemented": "yes", "configured": True}]}]}
    capability = CaseView.from_payload(payload).solver("telemac").capability("ice_jam")
    assert capability.visible and capability.title == "Ice jam"


# ------------------------------------------------- Qt6 and security regressions
#
# Both classes of problem below were reported by plugins.qgis.org's own analysis on
# upload, which is a slow way to find out. These run them locally instead.


#: Enums that Qt6 requires to be written scoped. QGIS's checker reads the source and
#: flags the unscoped literal, and on Qt6 the unscoped spelling genuinely stops working -
#: so the literal must not appear outside compat.py, which resolves it by name for both.
UNSCOPED_ENUMS = [
    (r"QgsUnitTypes\.LayoutMillimeters", "QgsUnitTypes.LayoutUnit.LayoutMillimeters"),
    (r"QgsLayoutItemPicture\.FormatSVG", "QgsLayoutItemPicture.Format.FormatSVG"),
    (r"QgsTask\.CanCancel", "QgsTask.Flag.CanCancel"),
    (r"QgsColorRampShader\.Interpolated", "QgsColorRampShader.Type.Interpolated"),
    (r"QgsLegendStyle\.(Title|Group|Subgroup|SymbolLabel)", "QgsLegendStyle.Style.*"),
    (r"Qt\.(UserRole|AlignRight|AlignCenter|MatchExactly)\b", "Qt.<Scope>.*"),
]


@pytest.mark.parametrize("pattern,scoped", UNSCOPED_ENUMS)
def test_no_unscoped_qt_enum_outside_compat(pattern, scoped):
    """compat.py is the one file allowed to name both spellings - that is its job."""
    import re

    bad = re.compile(pattern)
    offenders = []
    for path in (PLUGIN_ROOT / "axqua").rglob("*.py"):
        if path.name == "compat.py":
            continue
        for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if bad.search(line):
                offenders.append(f"{path.relative_to(PLUGIN_ROOT)}:{n}")
    assert offenders == [], (f"unscoped enum; use the scoped form ({scoped}) via "
                            f"compat: {offenders}")


def test_no_exec_underscore_outside_compat():
    """``exec_`` was removed in Qt6. compat routes it through getattr, so the literal
    member call should exist nowhere else."""
    import re

    offenders = [str(p.relative_to(PLUGIN_ROOT))
                 for p in (PLUGIN_ROOT / "axqua").rglob("*.py")
                 if p.name != "compat.py" and re.search(r"\.exec_\(", p.read_text("utf-8"))]
    assert offenders == []


def test_optional_enums_degrade_instead_of_raising():
    """``enum_value(..., default=None)`` is what stops a member that a given QGIS lacks
    from breaking the import - and with it the whole plugin - rather than costing one
    styling nicety. QgsMeshRendererVectorSettings.ColoringMethod is exactly that case.
    """
    if importlib.util.find_spec("qgis") is None:
        pytest.skip("compat imports PyQGIS, which is not available here")

    from axqua_plugin.compat import enum_value          # noqa: PLC0415

    class Empty:
        pass

    assert enum_value(Empty, "Nope.Nope", default=None) is None
    with pytest.raises(AttributeError):
        enum_value(Empty, "Nope.Nope")


def test_the_plugin_passes_a_security_scan():
    """The same bandit analysis plugins.qgis.org runs on upload.

    Every subprocess call here passes an argument **list** with no shell, which is what
    makes running a user-configured executable safe; the ``# nosec`` markers record that
    reasoning at the call site rather than hiding it in a config file.
    """
    import subprocess
    import sys

    if importlib.util.find_spec("bandit") is None:
        pytest.skip("bandit is not installed")
    proc = subprocess.run([sys.executable, "-m", "bandit", "-r",
                           str(PLUGIN_ROOT / "axqua"), "-q", "-f", "json"],
                          capture_output=True, text=True)
    results = json.loads(proc.stdout or "{}").get("results", [])
    assert results == [], "\n".join(
        f"{r['filename']}:{r['line_number']} [{r['test_id']}] {r['issue_text']}"
        for r in results)
