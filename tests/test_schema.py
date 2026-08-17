"""Phase 4: the config schema - round-tripping it, classifying it, and its errors."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from axqua.core import errors, schema

CASES = sorted((Path(__file__).resolve().parent.parent / "cases").glob(
    "*/case-config*.yml"))


# --------------------------------------------------------------------------- #
# round-trip: load -> dump -> load
# --------------------------------------------------------------------------- #

def _diff(a, b, prefix=""):
    """Field-by-field difference between two Configs, ignoring bookkeeping."""
    out = []
    for f in fields(a):
        if f.name in ("config_dir", "declared_blocks"):
            continue
        x, y = getattr(a, f.name), getattr(b, f.name)
        if is_dataclass(x) and is_dataclass(y):
            out += _diff(x, y, f"{prefix}{f.name}.")
        elif x != y:
            out.append(f"{prefix}{f.name}: {x!r} != {y!r}")
    return out


@pytest.mark.parametrize("path", CASES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_case_config_round_trips(path, tmp_path):
    """`load_config(dump_config(cfg)) == cfg` for every config in the repo.

    This is what makes ``dump_config`` safe to point at a real case: a form-based
    editor that writes a config back must not quietly drop a field it did not know
    about, and there is no way to be sure of that by reading the code.
    """
    from axqua.config import dump_config, load_config

    original = load_config(path)
    # dumped into the case dir, so relative data paths still resolve
    out = path.parent / "._roundtrip-test.yml"
    try:
        dump_config(original, out)
        reloaded = load_config(out)
    finally:
        out.unlink(missing_ok=True)
    assert _diff(original, reloaded) == []


def test_dump_writes_paths_relative_to_where_the_config_will_live(tmp_path):
    """A dumped config must stay portable: a case ships its data under its own
    directory, so those paths are written relative and an install path is not."""
    from axqua.config import dump_config, load_config

    cfg = load_config(CASES[0])
    text = dump_config(cfg, base=cfg.config_dir)
    assert str(cfg.config_dir) not in text          # nothing machine-local leaked in
    assert "axqua-case" in text                 # the case tree stayed relative


# --------------------------------------------------------------------------- #
# the shared-intent classification
# --------------------------------------------------------------------------- #

def test_the_reach_is_shared_and_solver_keywords_are_not():
    """The rule the table exists to state: a fact about the river is shared, a
    keyword of one code belongs to that code."""
    assert schema.classify("geodata.dem_initial") is schema.Layer.SHARED
    assert schema.classify("boundaries.prescribed_flowrate") is schema.Layer.SHARED
    assert schema.classify("friction.roughness_law") is schema.Layer.SHARED
    assert schema.classify("mesh.channel_size") is schema.Layer.SHARED
    assert schema.classify("hydrodynamics.duration") is schema.Layer.SHARED

    assert schema.classify("mesh.growth_ratio") is schema.Layer.TELEMAC     # BAMG
    assert schema.classify("hydrodynamics.implicitation") is schema.Layer.TELEMAC
    assert schema.classify("telemac.pysource") is schema.Layer.TELEMAC
    assert schema.classify("openfoam.max_courant") is schema.Layer.OPENFOAM
    assert schema.classify("project.name") is schema.Layer.PROJECT


def test_an_unknown_key_is_shared_rather_than_hidden():
    """Failing safe for a form: showing a setting on the reach page is a smaller
    mistake than hiding it behind a solver the user has not enabled."""
    assert schema.classify("something_new.field") is schema.Layer.SHARED


def test_every_classified_block_is_a_real_config_block():
    """The table must not drift away from the dataclass it describes."""
    from axqua.config import Config

    known = {f.name for f in fields(Config)} | {"project", "outputs"}
    for block in list(schema.BLOCK_LAYER) + list(schema.SHARED_FIELDS):
        assert block in known, f"schema names a block that does not exist: {block}"


def test_every_shared_field_exists_on_its_block():
    from axqua.config import Hydrodynamics, MeshConfig

    blocks = {"mesh": MeshConfig, "hydrodynamics": Hydrodynamics}
    for block, names in schema.SHARED_FIELDS.items():
        real = {f.name for f in fields(blocks[block])}
        missing = set(names) - real
        assert not missing, f"{block}: schema names fields that do not exist: {missing}"


# --------------------------------------------------------------------------- #
# legacy keys
# --------------------------------------------------------------------------- #

def test_legacy_keys_are_renamed_and_reported_once():
    raw = {"project": {"work_dir": "old/pre", "results_dir": "old/cal"}}
    renamed = schema.apply_legacy(raw)

    assert raw["project"]["preprocessing_dir"] == "old/pre"
    # results_dir became calibration_dir, NOT model_dir
    assert raw["project"]["calibration_dir"] == "old/cal"
    assert "work_dir" not in raw["project"]
    assert len(renamed) == 2


def test_an_explicit_canonical_key_beats_the_legacy_one():
    """Someone mid-migration may have both; the new spelling is the intended one."""
    raw = {"project": {"work_dir": "old", "preprocessing_dir": "new"}}
    schema.apply_legacy(raw)
    assert raw["project"]["preprocessing_dir"] == "new"


def test_a_legacy_config_still_loads(tmp_path):
    """End to end through load_config, which is where it has to keep working."""
    from axqua.config import load_config

    (tmp_path / "pysource.sh").write_text("# stub\n")
    (tmp_path / "dem.tif").write_text("")
    (tmp_path / "roi.gpkg").write_text("")
    (tmp_path / "legacy.yml").write_text(
        "project:\n"
        "  name: legacy\n"
        "  work_dir: my-preprocessing\n"
        "  results_dir: my-calibration\n"
        "telemac:\n"
        "  pysource: pysource.sh\n"
        "geodata:\n"
        "  dem_initial: dem.tif\n"
        "  boundary: roi.gpkg\n"
    )
    cfg = load_config(tmp_path / "legacy.yml")
    assert cfg.preprocessing_dir == tmp_path / "my-preprocessing"
    assert cfg.calibration_dir == tmp_path / "my-calibration"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

def test_an_error_carries_a_stable_code_and_survives_serialisation():
    """The code is the contract - a caller branches on it and a job record stores
    it, so it must not change when the wording does."""
    import json

    exc = errors.ConfigError("outflow_condition must be one of ...",
                             subject="boundaries.outflow_condition",
                             remedy="Use 'elevation', 'stage_discharge' or 'free'.",
                             given="rating")
    data = exc.as_dict()
    assert data["code"] == "axqua.config"
    assert data["subject"] == "boundaries.outflow_condition"
    assert json.loads(json.dumps(data))["details"]["given"] == "rating"
    assert "Use 'elevation'" in str(exc)      # the remedy reaches the reader


def test_every_error_is_catchable_by_the_base_class():
    for cls in (errors.ConfigError, errors.GeodataError, errors.EnvironmentError,
                errors.SolverError, errors.MeshError):
        with pytest.raises(errors.AxquaError):
            raise cls("boom")


def test_error_codes_are_unique():
    codes = [c.code for c in (errors.AxquaError, errors.ConfigError,
                              errors.GeodataError, errors.EnvironmentError,
                              errors.SolverError, errors.MeshError)]
    assert len(codes) == len(set(codes))


def test_an_unexpected_exception_is_recorded_honestly():
    """A job that died on a KeyError deep in a library still records something
    structured - labelled as unanticipated rather than mislabelled."""
    record = errors.ErrorRecord.from_exception(KeyError("Zone Name"))
    assert record.code == "axqua.unexpected"
    assert record.details["type"] == "KeyError"

    record = errors.ErrorRecord.from_exception(
        errors.MeshError("BAMG failed", subject="mesh.channel_size"))
    assert record.code == "axqua.mesh"
    assert record.as_dict()["subject"] == "mesh.channel_size"
