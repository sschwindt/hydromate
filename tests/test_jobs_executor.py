"""Executing a job: dispatch, state sequence, failure recording, cancellation.

The dispatch tests use a **fake backend registered through the real registry**, which is
the point: if the executor could only be tested against TELEMAC then the "no solver
branching in the core" rule (plan §12) would be untested, and the first thing to break it
would go unnoticed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from hydromate.core import registry
from hydromate.core.capabilities import Capability, Support
from hydromate.core.errors import SolverError
from hydromate.core.registry import BackendSpec, BaseBackend, CapabilitySpec
from hydromate.jobs import executor, submit as submit_mod
from hydromate.jobs.model import JobKind, JobState
from hydromate.jobs.store import read_status


class RecordingBackend(BaseBackend):
    """Records what it was asked to do, and can be told to fail."""

    name = "faketelemac"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Capability | None, dict]] = []
        self.fail_with: Exception | None = None
        self.exported: list[str] = []

    def check_environment(self, cfg) -> bool:
        return True

    def _record(self, verb, cfg, capability, ctx, kwargs):
        self.calls.append((verb, capability, dict(kwargs)))
        if self.fail_with is not None:
            raise self.fail_with
        if ctx is not None:
            ctx.progress(iteration=7)
        return {"verb": verb}

    def build(self, cfg, capability=None, *, ctx=None, **kw):
        return self._record("build", cfg, capability, ctx, kw)

    def run(self, cfg, capability=None, *, ctx=None, **kw):
        return self._record("run", cfg, capability, ctx, kw)

    def study(self, cfg, capability=None, *, ctx=None, **kw):
        return self._record("study", cfg, capability, ctx, kw)

    def postprocess(self, cfg, ctx):
        self.calls.append(("postprocess", None, {}))

    def extract_objective(self, cfg, ctx):
        return 0.0472

    def export_qgis_results(self, cfg, ctx):
        self.exported.append("called")
        return []


class _FakeSpec(BackendSpec):
    """A spec that hands back a live object.

    A subclass rather than a monkeypatch because ``BackendSpec`` is a *frozen* dataclass
    - which is itself the right design (a spec is metadata and should not be mutable),
    so the test bends to it.
    """

    #: Keyed by solver name so the fixture can swap the instance per test without
    #: touching the frozen spec.
    instances: dict[str, object] = {}

    def load(self):
        return self.instances[self.name]


@pytest.fixture
def fake_backend():
    """Register a fake solver under the telemac name, and clean up after.

    Through the *real* registry, so the executor's "no solver branching" rule is
    genuinely exercised rather than assumed.
    """
    backend = RecordingBackend()
    _FakeSpec.instances["telemac"] = backend
    spec = _FakeSpec(
        name="telemac", title="fake", config_key="telemac",
        capabilities={cap: CapabilitySpec(support=Support.SUPPORTED)
                      for cap in Capability},
        enabled=lambda cfg: True,
    )
    previous = registry._REGISTRY.get("telemac")
    registry.register(spec, replace=True)
    yield backend
    _FakeSpec.instances.pop("telemac", None)
    if previous is not None:
        registry.register(previous, replace=True)
    else:                                            # pragma: no cover
        registry._REGISTRY.pop("telemac", None)


def _submit(cfg, kind, **kwargs):
    return submit_mod.submit_job(cfg, kind, validate_env=False, dry_run=True, **kwargs)


def _create(cfg, kind, **kwargs):
    spec = submit_mod.build_spec(cfg, kind, **kwargs)
    return submit_mod.create_job(cfg, spec)


# -------------------------------------------------------------------------- dispatch


@pytest.mark.parametrize("kind,verb", [
    (JobKind.PREPROCESSING, "build"),
    (JobKind.STEADY_RUN, "run"),
    (JobKind.MESH_CONVERGENCE, "study"),
    (JobKind.CALIBRATION, "study"),
])
def test_the_kind_decides_the_backend_verb(fake_case, fake_backend, kind, verb):
    jd = _create(fake_case, kind)
    assert executor.execute(jd) == 0
    assert fake_backend.calls[0][0] == verb


def test_a_successful_job_walks_the_whole_state_sequence(fake_case, fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    status = read_status(jd)
    assert status.state is JobState.COMPLETED
    assert [h["state"] for h in status.history] == \
        ["QUEUED", "STARTING", "RUNNING", "POSTPROCESSING", "COMPLETED"]
    assert status.exit_code == 0


def test_progress_reaches_status_json(fake_case, fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    progress = read_status(jd).progress
    assert progress.as_dict()["iteration"] == 7


def test_the_result_manifest_is_written_with_the_objective(fake_case, fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    payload = json.loads(jd.results_json.read_text(encoding="utf-8"))
    assert payload["objective"] == pytest.approx(0.0472)
    assert payload["job_id"] == jd.job_id
    assert fake_backend.exported == ["called"]


def test_the_options_reach_the_backend(fake_case, fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN, options={"ncsize": 6})
    executor.execute(jd)
    assert fake_backend.calls[0][2]["ncsize"] == 6


# --------------------------------------------------------------------------- failure


def test_a_solver_failure_is_recorded_with_its_stable_code(fake_case, fake_backend):
    fake_backend.fail_with = SolverError("telemac2d exited with code 1",
                                         return_code=1, component="telemac2d",
                                         remedy="Read the listing.")
    jd = _create(fake_case, JobKind.STEADY_RUN)
    assert executor.execute(jd) == 1
    status = read_status(jd)
    assert status.state is JobState.FAILED
    assert status.error["code"] == "hydromate.solver"
    assert status.error["remedy"] == "Read the listing."
    assert status.exit_code == 1


def test_the_failure_is_in_the_runner_log_as_well(fake_case, fake_backend):
    """Plan §24 requires both: the record is what a GUI acts on, the log is what a
    person reads."""
    fake_backend.fail_with = SolverError("it went wrong", component="telemac2d")
    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    assert "it went wrong" in jd.runner_log.read_text(encoding="utf-8")


def test_an_unexpected_exception_is_labelled_honestly(fake_case, fake_backend):
    """Not mislabelled as a solver error - 'hydromate did not anticipate this' is the
    truthful code."""
    fake_backend.fail_with = KeyError("surprise")
    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    assert read_status(jd).error["code"] == "hydromate.unexpected"


def test_a_missing_file_gets_a_config_code_and_a_remedy(fake_case, fake_backend):
    fake_backend.fail_with = FileNotFoundError("no such file: geometry.slf")
    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    error = read_status(jd).error
    assert error["code"] == "hydromate.config"
    assert "case-config.yml" in error["remedy"]


def test_a_postprocessing_failure_does_not_discard_a_successful_run(fake_case,
                                                                   fake_backend,
                                                                   monkeypatch):
    """Hours of solver time are worth more than a missing figure."""
    def explode(cfg, ctx):
        raise RuntimeError("the plot library fell over")
    monkeypatch.setattr(fake_backend, "postprocess", explode)

    jd = _create(fake_case, JobKind.STEADY_RUN)
    assert executor.execute(jd) == 0
    assert read_status(jd).state is JobState.COMPLETED
    payload = json.loads(jd.results_json.read_text(encoding="utf-8"))
    assert any("postprocess" in w for w in payload["summary"]["warnings"])


# ---------------------------------------------------------------------- cancellation


def test_a_cancel_request_present_before_the_start_settles_immediately(fake_case,
                                                                      fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN)
    jd.cancel_request.write_text("{}", encoding="utf-8")
    assert executor.execute(jd) == 0
    assert read_status(jd).state is JobState.CANCELLED
    assert fake_backend.calls == []


def test_a_cancel_noticed_mid_run_ends_as_cancelled(fake_case, fake_backend,
                                                    monkeypatch):
    def cancel_midway(cfg, capability=None, *, ctx=None, **kw):
        jd_ = ctx.job_dir
        jd_.cancel_request.write_text("{}", encoding="utf-8")
        ctx.cancel.interval = 0.0        # do not wait for the poll window
        ctx.check_cancelled()
    monkeypatch.setattr(fake_backend, "run", cancel_midway)

    jd = _create(fake_case, JobKind.STEADY_RUN)
    assert executor.execute(jd) == 130
    status = read_status(jd)
    assert status.state is JobState.CANCELLED
    assert "CANCEL_REQUESTED" in [h["state"] for h in status.history]


# ------------------------------------------------------------------------ workspace


def test_a_build_kind_rebases_into_the_job_directory(fake_case, fake_backend):
    """A build produces its own inputs, so a self-contained directory is right."""
    captured = {}

    def capture(cfg, capability=None, *, ctx=None, **kw):
        captured["model_dir"] = Path(cfg.model_dir)
    fake_backend.build = capture

    jd = _create(fake_case, JobKind.PREPROCESSING)
    executor.execute(jd)
    assert captured["model_dir"] == jd.simulation


def test_a_run_kind_stays_in_the_case_folders(fake_case, fake_backend):
    """A run hotstarts from a result already sitting in the case's model_dir; copying
    it would violate the large-data rule."""
    captured = {}
    original = Path(fake_case.model_dir)

    def capture(cfg, capability=None, *, ctx=None, **kw):
        captured["model_dir"] = Path(cfg.model_dir)
    fake_backend.run = capture

    jd = _create(fake_case, JobKind.STEADY_RUN)
    executor.execute(jd)
    assert captured["model_dir"] == original


# ---------------------------------------------------------------- the study question


def test_a_study_never_reads_stdin(fake_case, fake_backend, monkeypatch):
    """The executor always injects ``ask``, so the study's own stdin prompt is
    unreachable inside a job. Monkeypatched to raise, so a regression is loud."""
    import hydromate.convergence as convergence

    def forbidden(*args, **kwargs):
        raise AssertionError("a detached job asked stdin a question")
    monkeypatch.setattr(convergence, "_default_ask", forbidden, raising=False)

    answers = {}

    def study(cfg, capability=None, *, ctx=None, **kw):
        answers["resume"] = ctx.ask("Reuse the 2 completed levels?")
        answers["extend"] = ctx.ask("Refine one level further?")
    fake_backend.study = study

    jd = _create(fake_case, JobKind.MESH_CONVERGENCE,
                 options={"answers": {"resume": True, "extend": True}})
    executor.execute(jd)
    assert answers == {"resume": True, "extend": True}
    assert "Refine one level further?" in jd.runner_log.read_text(encoding="utf-8")


# -------------------------------------------------------------------- the frozen job


def test_the_frozen_config_reloads_to_the_same_case(fake_case, fake_backend):
    """``input/case-config.yml`` is what the executor actually loads, so reloading it
    must give back the same case - same settings and, critically, the same **resolved**
    data paths.

    The last part is not automatic. ``dump_config`` writes a path relative to the file's
    own directory when it sits underneath it and absolute otherwise; a case whose geodata
    lives outside the job directory therefore has to come out absolute, or the frozen
    config would point at files inside the job that were never copied there.
    """
    from hydromate.config import load_config
    from hydromate.jobs.store import read_spec

    jd = _create(fake_case, JobKind.STEADY_RUN)
    reloaded = load_config(jd.frozen_config)

    assert reloaded.name == fake_case.name
    assert reloaded.boundaries.prescribed_flowrate == \
        fake_case.boundaries.prescribed_flowrate
    assert reloaded.hydrodynamics.duration == fake_case.hydrodynamics.duration
    # The geodata lives outside the job, so its path must have survived as an absolute
    # one rather than being rewritten into a directory that holds no data.
    assert Path(reloaded.geodata.boundary) == Path(fake_case.geodata.boundary)
    assert Path(reloaded.telemac.pysource) == Path(fake_case.telemac.pysource)

    # And the record agrees with the file about what was asked for.
    recorded = read_spec(jd).case["config"]
    assert recorded["boundaries"]["prescribed_flowrate"] == \
        fake_case.boundaries.prescribed_flowrate


def test_editing_the_case_afterwards_does_not_change_a_created_job(fake_case,
                                                                   fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN)
    original = jd.frozen_config.read_text(encoding="utf-8")
    (Path(fake_case.config_dir) / "case-config.yml").write_text(
        "project:\n  name: rewritten\n", encoding="utf-8")
    assert jd.frozen_config.read_text(encoding="utf-8") == original


def test_the_submission_record_is_read_only(fake_case, fake_backend):
    jd = _create(fake_case, JobKind.STEADY_RUN)
    mode = jd.spec.stat().st_mode
    assert not mode & 0o222, "job.json should not be writable"


# ------------------------------------------------------------- architectural guards


def test_nothing_in_jobs_imports_a_solver_except_the_executor():
    """The job system is solver-agnostic; only the executor may dispatch to one, and
    only through the registry. Checked by reading the source, so a function-local
    import cannot slip through."""
    pattern = re.compile(r"(from|import)\s+hydromate\.solvers")
    root = Path(sys.modules["hydromate"].__file__).parent / "jobs"
    offenders = [p.relative_to(root) for p in root.rglob("*.py")
                 if p.name != "executor.py" and pattern.search(p.read_text("utf-8"))]
    assert offenders == []
