"""The TELEMAC pre-run that seeds an OpenFOAM case.

No solver and no TELEMAC install: ``pipeline.run`` is monkeypatched, so what is under
test is the *decision* - when a pre-run happens, what config the coarse copy is built
with, and where its products land. Those are the parts that go wrong silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hydromate import prerun


# --------------------------------------------------------------------------- #
# a config stub with just the surface ensure_seed touches
# --------------------------------------------------------------------------- #

def _cfg(tmp_path: Path, **pre_kw):
    from hydromate.config import (
        Hydrodynamics, Initialization, MeshConfig, OpenFoam, PreRun, TelemacEnv,
    )

    class _Cfg:
        results_slf = "r2d.slf"
        cas_file = "steady2d.cas"

        def __init__(self):
            self.model_dir = tmp_path / "simulation"
            self.preprocessing_dir = tmp_path / "preprocessing"
            self.postprocessing_dir = tmp_path / "postprocessing"
            self.calibration_dir = tmp_path / "calibration"
            self.mesh = MeshConfig()
            self.hydrodynamics = Hydrodynamics()
            self.initialization = Initialization()
            self.telemac = TelemacEnv(pysource=tmp_path / "pysource.sh")
            self.openfoam = OpenFoam(pre_run=PreRun(**pre_kw))
            self.made_dirs = False

        def model_path(self, name):
            return self.model_dir / name

        def ensure_dirs(self):
            self.made_dirs = True
            self.model_dir.mkdir(parents=True, exist_ok=True)

    cfg = _Cfg()
    cfg.model_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def spy(monkeypatch):
    """Stand in for pipeline.run, recording the config it was handed."""
    calls: list = []

    def fake_run(lc, **kwargs):
        calls.append((lc, kwargs))
        lc.model_dir.mkdir(parents=True, exist_ok=True)
        (lc.model_dir / lc.results_slf).write_bytes(b"")   # the "result"
        return None

    import hydromate.solvers.telemac.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run", fake_run)
    return calls


# --------------------------------------------------------------------------- #
# the toggle
# --------------------------------------------------------------------------- #

def test_disabled_never_starts_a_solver(tmp_path, spy):
    """Unchecking the box must not run TELEMAC, even with nothing to seed from."""
    result = prerun.ensure_seed(_cfg(tmp_path, enabled=False))

    assert spy == []
    assert result.source == prerun.DISABLED
    assert result.path is None and not result.ok
    assert "COLD" in " ".join(result.summary())


def test_disabled_still_uses_a_result_that_is_already_there(tmp_path, spy):
    """A pre-run that is switched off is not a reason to ignore a free seed."""
    cfg = _cfg(tmp_path, enabled=False)
    (cfg.model_dir / "r2d.slf").write_bytes(b"")

    result = prerun.ensure_seed(cfg)
    assert spy == []
    assert result.ok and result.source == prerun.DISABLED


def test_per_call_override_beats_the_config(tmp_path, spy):
    """`--no-pre-run` / the case script's module-level switch."""
    result = prerun.ensure_seed(_cfg(tmp_path, enabled=True), enabled=False)
    assert spy == [] and result.source == prerun.DISABLED


# --------------------------------------------------------------------------- #
# reuse before re-running
# --------------------------------------------------------------------------- #

def test_an_existing_result_is_reused_rather_than_recomputed(tmp_path, spy):
    """Hours of converged fine-grid compute beat a fresh coarse run every time."""
    cfg = _cfg(tmp_path)
    (cfg.model_dir / "r2d.slf").write_bytes(b"")

    result = prerun.ensure_seed(cfg)
    assert spy == []
    assert result.source == prerun.EXISTING
    assert result.path == cfg.model_dir / "r2d.slf"


def test_reuse_false_forces_a_fresh_pre_run(tmp_path, spy):
    cfg = _cfg(tmp_path, reuse=False)
    (cfg.model_dir / "r2d.slf").write_bytes(b"")

    result = prerun.ensure_seed(cfg)
    assert len(spy) == 1
    assert result.source == prerun.PRE_RUN


# --------------------------------------------------------------------------- #
# what the coarse copy is actually built with
# --------------------------------------------------------------------------- #

def test_the_coarse_copy_scales_the_whole_sizing_field(tmp_path, spy):
    """size_scale, not channel_size: the per-zone `Max Edge Length (m)` values in the
    mesh-zones gpkg override the config sizes, so scaling those alone meshes every
    "coarse" run at the baseline resolution."""
    cfg = _cfg(tmp_path, size_scale=3.0)
    cfg.mesh.size_scale = 2.0                       # already coarsened by the caller

    prerun.ensure_seed(cfg)
    (lc, _), = spy
    assert lc.mesh.size_scale == pytest.approx(6.0)   # multiplied, not replaced
    assert cfg.mesh.size_scale == pytest.approx(2.0)  # the caller's config is untouched


def test_the_pre_run_is_prewetted_and_marches_faster(tmp_path, spy):
    """It exists to find a surface, not to reproduce the production dry start."""
    cfg = _cfg(tmp_path, prewet_depth=0.4, courant=0.7)

    prerun.ensure_seed(cfg)
    (lc, _), = spy
    assert lc.initialization.prewet_depth == pytest.approx(0.4)
    assert lc.hydrodynamics.desired_courant == pytest.approx(0.7)
    assert lc.hydrodynamics.variable_timestep is True


def test_the_pre_run_writes_into_its_own_folder_and_leaves_the_case_alone(tmp_path,
                                                                         spy):
    """A seed needs a mesh, a steering file and a result - not a DEM clip, a ground
    truth table or a calibration config, and certainly not on top of the real ones."""
    cfg = _cfg(tmp_path, directory="pre-run")

    result = prerun.ensure_seed(cfg)
    (lc, kwargs), = spy
    assert lc.model_dir == cfg.model_dir / "pre-run"
    assert result.path == cfg.model_dir / "pre-run" / "r2d.slf"
    for scratch in (lc.preprocessing_dir, lc.postprocessing_dir, lc.calibration_dir):
        assert scratch != cfg.preprocessing_dir
        assert not scratch.exists()          # the temp dir is removed afterwards
    assert kwargs["dry_run"] is True         # build AND run, in one call
    assert kwargs["log_to_file"] is False    # into the caller's compound log


def test_a_failed_pre_run_does_not_lose_the_build(tmp_path, monkeypatch):
    """A cold build is worse, not fatal. Losing the whole OpenFOAM build because the
    optional seed failed would be the wrong trade."""
    import hydromate.solvers.telemac.pipeline as pipeline

    def boom(lc, **kwargs):
        raise RuntimeError("BAMG gave up")

    monkeypatch.setattr(pipeline, "run", boom)
    result = prerun.ensure_seed(_cfg(tmp_path))

    assert result.source == prerun.NONE and not result.ok
    assert "BAMG gave up" in " ".join(result.notes)


def test_require_error_stops_on_an_unconverged_pre_run(tmp_path, spy, monkeypatch):
    """`warn` seeds from a transient surface and says so; `error` is for a workflow
    that must not build on one."""
    monkeypatch.setattr(prerun, "_judge",
                        lambda result, model_dir, cfg: _unconverged(result))

    warned = prerun.ensure_seed(_cfg(tmp_path, require="warn"))
    assert warned.ok and warned.converged is False
    assert "NOT reached flux balance" in " ".join(warned.summary())

    with pytest.raises(RuntimeError, match="did not reach flux balance"):
        prerun.ensure_seed(_cfg(tmp_path, require="error"))


def _unconverged(result):
    result.converged = False
    result.imbalance = 0.42
    return result


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def test_pre_run_is_on_by_default_and_validates_its_own_fields():
    from hydromate.config import OpenFoam, PreRun

    assert OpenFoam().pre_run.enabled is True
    assert OpenFoam().pre_run.dimension == "2d"
    with pytest.raises(ValueError, match="dimension"):
        PreRun(dimension="2.5d").validate()
    with pytest.raises(ValueError, match="require"):
        PreRun(require="maybe").validate()
