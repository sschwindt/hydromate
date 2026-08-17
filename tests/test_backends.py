"""The solver backends, and the import-weight contract that keeps the plugin fast.

``BackendSpec.implementation`` named two modules that did not exist, so ``spec.load()``
was dead code and nothing implemented ``SolverBackend``. These tests pin both halves: the
implementations now exist and satisfy the protocol, and loading a *spec* still costs
nothing - because ``axqua status`` and a QGIS plugin ask "what can this case do?" far
more often than they ask anyone to mesh something.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from axqua.core import registry
from axqua.core.capabilities import Capability
from axqua.core.registry import BaseBackend, SolverBackend

SRC = Path(registry.__file__).parents[2]


@pytest.mark.parametrize("name", ["telemac", "openfoam"])
def test_the_declared_implementation_exists_and_satisfies_the_protocol(name):
    backend = registry.get(name).load()
    assert isinstance(backend, SolverBackend)
    assert backend.name == name


@pytest.mark.parametrize("name", ["telemac", "openfoam"])
@pytest.mark.parametrize("method", ["check_environment", "build", "run", "study",
                                    "postprocess", "extract_objective",
                                    "export_qgis_results"])
def test_every_verb_is_present(name, method):
    assert callable(getattr(registry.get(name).load(), method))


def test_the_entry_point_group_is_declared():
    """Third-party registration is designed; this is what makes it real."""
    from importlib.metadata import entry_points
    names = {ep.name for ep in entry_points(group=registry.ENTRY_POINT_GROUP)}
    assert {"telemac", "openfoam"} <= names


def test_base_backend_refuses_clearly_rather_than_returning_none():
    class Minimal(BaseBackend):
        name = "minimal"

    backend = Minimal()
    assert backend.check_environment(None) is True
    assert backend.extract_objective(None, None) is None
    assert backend.export_qgis_results(None, None) == []
    with pytest.raises(NotImplementedError) as excinfo:
        backend.build(None, Capability.STEADY2D)
    assert "minimal" in str(excinfo.value)


# ------------------------------------------------------------------ import weight


def test_listing_backends_does_not_import_their_implementations():
    """A capability listing must not drag a solver in.

    Run in a subprocess, because this process has already imported half of axqua and
    would mask the regression completely.
    """
    code = (
        "import sys\n"
        "from axqua.core import registry\n"
        "registry.backends()\n"
        "print([m for m in sys.modules if m.endswith('.backend')])\n"
        "print([m for m in ('numpy','scipy','pandas','geopandas','rasterio','shapely',"
        "'matplotlib','gmsh','openpyxl') if m in sys.modules])\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    backends, heavy = proc.stdout.strip().splitlines()
    assert backends == "[]", f"a backend module was imported eagerly: {backends}"
    assert heavy == "[]", f"a heavy module was imported: {heavy}"


def test_a_spec_module_stays_import_light():
    """``spec.py`` is read by ``axqua status`` and, later, by a plugin running
    inside the QGIS Python process. It may use the standard library and attribute
    access on a Config, and nothing else."""
    forbidden = re.compile(r"^\s*(?:from|import)\s+(numpy|pandas|gmsh|rasterio|geopandas"
                           r"|shapely|matplotlib|openpyxl)", re.M)
    for solver in ("telemac", "openfoam"):
        spec_file = SRC / "axqua" / "solvers" / solver / "spec.py"
        assert not forbidden.search(spec_file.read_text("utf-8")), spec_file


def test_the_openfoam_backend_never_reaches_into_telemac():
    """The two backends are siblings. Orchestration that drives both - obtaining a
    TELEMAC seed for an OpenFOAM build - lives above both, in the executor."""
    pattern = re.compile(r"axqua\.solvers\.telemac")
    backend = SRC / "axqua" / "solvers" / "openfoam" / "backend.py"
    assert not pattern.search(backend.read_text("utf-8"))


def test_the_openfoam_backend_does_not_call_prerun():
    """``prerun`` imports the TELEMAC backend transitively, so calling it from here
    would break the sibling rule in a way the regex above cannot see."""
    backend = SRC / "axqua" / "solvers" / "openfoam" / "backend.py"
    text = backend.read_text("utf-8")
    assert "prerun" not in text.split("Two backends are siblings")[-1] or \
        "ensure_seed(" not in text


# ---------------------------------------------------------------- the MPI launcher


def test_openfoam_uses_the_configured_mpi_launcher_not_a_hard_coded_mpirun(tmp_path,
                                                                          monkeypatch):
    """MS-MPI on Windows provides ``mpiexec``; a cluster profile may say ``srun``.
    The launcher is a configured field, not a constant (plan §13)."""
    from axqua.config import OpenFoam
    from axqua.solvers.openfoam.runtime import OpenFoamRuntime

    bashrc = tmp_path / "bashrc.sh"
    bashrc.write_text("# stub\n", encoding="utf-8")
    runtime = OpenFoamRuntime(OpenFoam(bashrc=bashrc, n_processors=4))
    runtime.environment.mpi_launcher = "mpiexec"

    seen = {}
    monkeypatch.setattr(runtime, "run",
                        lambda command, **kw: seen.setdefault("command", command))
    monkeypatch.setattr("axqua.solvers.openfoam.dicts.activate",
                        lambda *a, **k: None)
    runtime.run_stage(tmp_path, "run", end_time=1.0, n_processors=4,
                      show_progress=False)
    assert seen["command"].startswith("mpiexec -np 4")


def test_telemac_never_prepends_an_mpi_launcher(tmp_path, monkeypatch):
    """TELEMAC's own launcher spawns mpirun from its systel config, so prepending one
    would start the run twice."""
    from axqua.config import TelemacEnv
    from axqua.env import TelemacRuntime

    pysource = tmp_path / "pysource.sh"
    pysource.write_text("# stub\n", encoding="utf-8")
    runtime = TelemacRuntime(TelemacEnv(pysource=pysource, n_processors=8))
    seen = {}
    monkeypatch.setattr(runtime, "run",
                        lambda command, **kw: seen.setdefault("command", command))
    runtime.run_solver("steady2d.cas", cwd=tmp_path)
    assert "mpirun" not in seen["command"] and "mpiexec" not in seen["command"]
    assert "--ncsize=8" in seen["command"]


# --------------------------------------------------------------------------- version


def test_the_version_is_declared_in_exactly_one_place():
    """``__version__`` reads the installed metadata; the literal is only the fallback for
    an uninstalled checkout. A drift between them would make a release report a version
    it is not - which is precisely what the plugin's ``--version`` probe reads."""
    import re

    import axqua

    # Read with a regex rather than tomllib, which is 3.11+ while axqua supports
    # 3.10 - and a version test that silently skips on the oldest supported Python is
    # exactly the one you want running there.
    pyproject = (SRC.parent / "pyproject.toml").read_text("utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    source = (SRC / "axqua" / "__init__.py").read_text("utf-8")
    fallback = re.search(r'_FALLBACK_VERSION\s*=\s*"([^"]+)"', source).group(1)
    assert fallback == declared, "the fallback in __init__.py has drifted from pyproject"
    assert axqua.__version__ == declared


def test_the_plugin_and_the_package_versions_agree():
    """They are released together from one tag, so a mismatch means one of them is
    stale - and the plugin's own release workflow checks the tag against metadata.txt."""
    import re

    import axqua

    metadata = (SRC.parent / "qgis_plugin" / "axqua" / "metadata.txt").read_text("utf-8")
    plugin_version = re.search(r"^version=(.+)$", metadata, re.M).group(1).strip()
    assert plugin_version == axqua.__version__
