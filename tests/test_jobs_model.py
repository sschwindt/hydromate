"""The job model: states, transitions, kinds, ids, options.

Pure data, so these are the tests that pin the contract everything else stands on.
"""

from __future__ import annotations

import datetime as dt

import pytest

from hydromate.core.capabilities import Capability
from hydromate.core.errors import ConfigError
from hydromate.jobs import ids
from hydromate.jobs.model import (KIND_META, TRANSITIONS, IllegalTransition, JobKind,
                                  JobSpec, JobState, JobStatus, Progress,
                                  SolverRunProgress, SteadyRunOptions, parse_kind)


# ----------------------------------------------------------------------- transitions


def test_every_state_appears_in_the_transition_table():
    assert set(TRANSITIONS) == set(JobState)


def test_terminal_states_go_nowhere():
    for state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
        assert state.is_terminal
        assert TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize("start,target", [
    (JobState.QUEUED, JobState.STARTING),
    (JobState.QUEUED, JobState.CANCELLED),          # cancelled before it ever launched
    (JobState.STARTING, JobState.RUNNING),
    (JobState.RUNNING, JobState.POSTPROCESSING),
    (JobState.POSTPROCESSING, JobState.COMPLETED),
    (JobState.RUNNING, JobState.CANCEL_REQUESTED),
    (JobState.CANCEL_REQUESTED, JobState.CANCELLED),
    (JobState.CANCEL_REQUESTED, JobState.COMPLETED),  # it finished before the signal
])
def test_legal_transitions(start, target):
    status = JobStatus(job_id="j", state=start)
    status.transition(target)
    assert status.state is target


@pytest.mark.parametrize("start,target", [
    (JobState.RUNNING, JobState.QUEUED),
    (JobState.COMPLETED, JobState.RUNNING),
    (JobState.CANCELLED, JobState.COMPLETED),
    (JobState.QUEUED, JobState.POSTPROCESSING),
    (JobState.FAILED, JobState.STARTING),
])
def test_illegal_transitions_raise_rather_than_being_coerced(start, target):
    """Clamping silently is how a job reports RUNNING forever, or COMPLETED after a
    crash."""
    status = JobStatus(job_id="j", state=start)
    with pytest.raises(IllegalTransition) as excinfo:
        status.transition(target)
    assert excinfo.value.code == "hydromate.job.transition"
    assert excinfo.value.details["target"] == target.value


def test_a_transition_to_the_same_state_is_a_no_op():
    status = JobStatus(job_id="j", state=JobState.RUNNING)
    status.transition(JobState.RUNNING)
    assert status.history == []          # not recorded twice


def test_history_and_timestamps_are_recorded():
    status = JobStatus(job_id="j")
    status.transition(JobState.STARTING)
    status.transition(JobState.RUNNING)
    status.transition(JobState.COMPLETED)
    assert [h["state"] for h in status.history] == ["STARTING", "RUNNING", "COMPLETED"]
    assert status.started and status.finished


# ---------------------------------------------------------------------------- kinds


def test_every_kind_has_metadata():
    assert set(KIND_META) == set(JobKind)


def test_kind_metadata_is_internally_consistent():
    """Each entry must name a real solver, a real capability and a real verb.

    This is the table the executor dispatches through, so a typo here would surface as a
    failing job rather than a failing import.
    """
    for kind, meta in KIND_META.items():
        assert meta.kind is kind
        assert meta.solver in {"telemac", "openfoam"}
        assert meta.verb in {"build", "run", "study"}
        assert meta.capability is None or isinstance(meta.capability, Capability)
        assert hasattr(meta.options, "from_dict")
        assert issubclass(meta.progress, Progress)
        assert meta.workspace_default in {"job", "case"}


def test_run_kinds_default_to_the_case_workspace():
    """A run hotstarts from a result in the case's own model_dir; copying it would
    violate the large-data rule."""
    assert KIND_META[JobKind.STEADY_RUN].workspace_default == "case"
    assert KIND_META[JobKind.PREPROCESSING].workspace_default == "job"


@pytest.mark.parametrize("text,expected", [
    ("steady", JobKind.STEADY_RUN),
    ("STEADY", JobKind.STEADY_RUN),
    ("steady_run", JobKind.STEADY_RUN),
    ("mesh-convergence", JobKind.MESH_CONVERGENCE),
    ("meshconv", JobKind.MESH_CONVERGENCE),
    ("bal", JobKind.CALIBRATION),
])
def test_parse_kind_accepts_the_obvious_spellings(text, expected):
    assert parse_kind(text) is expected


def test_parse_kind_names_the_alternatives_when_it_fails():
    with pytest.raises(ConfigError) as excinfo:
        parse_kind("nonsense")
    assert "steady" in (excinfo.value.remedy or "")


# ------------------------------------------------------------------------------ ids


def test_job_id_is_sortable_readable_and_matches_its_own_grammar():
    job_id = ids.make_job_id("Isar 2025!", "steady", when=dt.date(2026, 8, 14),
                             tail="a3f19c")
    assert job_id == "2026-08-14-isar-2025-steady-a3f19c"
    assert ids.is_job_id(job_id)
    date, slug, tail = ids.parse_job_id(job_id)
    assert (date, tail) == (dt.date(2026, 8, 14), "a3f19c")
    assert slug.startswith("isar-2025")


@pytest.mark.parametrize("bad", [
    "not-a-job", "2026-08-14-case", "/tmp/case-config.yml", "case-config.yml",
    "2026-8-14-case-abcdef", "2026-08-14-case-XYZXYZ",
])
def test_things_that_are_not_job_ids(bad):
    """The disambiguation rule in the CLI depends on this: a path must never parse."""
    assert not ids.is_job_id(bad)
    with pytest.raises(ConfigError):
        ids.parse_job_id(bad)


def test_slug_never_produces_an_empty_segment():
    assert ids.slug("!!!") == "case"
    assert ids.slug("") == "case"


# -------------------------------------------------------------------------- options


def test_options_reject_an_unknown_key():
    """Plan §5: never silently ignore an unknown field - that is how a job that looks
    submitted quietly runs something else."""
    with pytest.raises(ConfigError) as excinfo:
        SteadyRunOptions.from_dict({"ncsize": 4, "ncsizee": 8})
    assert "ncsizee" in str(excinfo.value)
    assert excinfo.value.subject == "options.ncsizee"


def test_options_round_trip():
    opts = SteadyRunOptions.from_dict({"ncsize": 4})
    assert opts.as_dict()["ncsize"] == 4
    assert SteadyRunOptions.from_dict(opts.as_dict()).ncsize == 4


# ------------------------------------------------------------------------- progress


def test_progress_is_typed_per_kind_and_survives_a_round_trip():
    progress = SolverRunProgress(simulated_time=125.0, duration=500.0, iteration=500)
    restored = Progress.from_dict(progress.as_dict())
    assert isinstance(restored, SolverRunProgress)
    assert restored.iteration == 500
    assert restored.fraction == pytest.approx(0.25)


def test_an_unknown_progress_kind_is_kept_rather_than_discarded():
    """Forward compatibility: a plugin reading a newer hydromate's status must degrade
    to showing nothing useful, not to losing the field."""
    payload = {"kind": "something_new", "widgets": 7}
    restored = Progress.from_dict(payload)
    assert restored == payload


def test_merged_ignores_none_so_a_partial_update_does_not_erase_fields():
    progress = SolverRunProgress(simulated_time=10.0, iteration=100)
    merged = progress.merged(simulated_time=20.0, iteration=None)
    assert (merged.simulated_time, merged.iteration) == (20.0, 100)


# ----------------------------------------------------------------------- the records


def test_job_spec_round_trips_through_json_form():
    spec = JobSpec(job_id="2026-08-14-x-steady-aaaaaa", kind=JobKind.STEADY_RUN,
                   solver="telemac", case={"name": "x"},
                   options=SteadyRunOptions(ncsize=8))
    restored = JobSpec.from_dict(spec.as_dict())
    assert restored.job_id == spec.job_id
    assert restored.kind is JobKind.STEADY_RUN
    assert restored.options.ncsize == 8


def test_job_spec_rejects_an_unknown_top_level_field():
    payload = JobSpec(job_id="i", kind=JobKind.STEADY_RUN, solver="telemac").as_dict()
    payload["surprise"] = 1
    with pytest.raises(ConfigError) as excinfo:
        JobSpec.from_dict(payload)
    assert "surprise" in str(excinfo.value)


def test_job_status_round_trips():
    spec = JobSpec(job_id="i", kind=JobKind.STEADY_RUN, solver="telemac")
    status = JobStatus.initial(spec)
    status.transition(JobState.STARTING)
    restored = JobStatus.from_dict(status.as_dict())
    assert restored.state is JobState.STARTING
    assert restored.kind is JobKind.STEADY_RUN
