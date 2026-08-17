"""Case capabilities, the solver registry and the ``MODEL=`` marker files.

Two things are pinned here that are easy to get quietly wrong:

* **the three axes stay separate.** "axqua cannot do this for this solver",
  "this case did not ask for it" and "it has not been built yet" are three different
  answers with three different fixes, and collapsing them into a single yes/no is the
  failure mode this module exists to prevent;
* **listing capabilities stays cheap.** The registry is lazy so that asking what a
  case can do never imports gmsh, rasterio, pandas or a solver. A future QGIS plugin
  asks exactly that question inside the QGIS Python process, so the test asserts the
  absence of those imports rather than trusting the design to hold.

Pure-python: no solver, no geodata, no config file on disk beyond a tmp YAML.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from axqua.core import registry
from axqua.core.capabilities import (
    Capability, CapabilityState, CaseStatus, SolverStatus, Support, read_marker,
)
from axqua.core.registry import BackendSpec, CapabilitySpec


# --------------------------------------------------------------------------- #
# the three axes
# --------------------------------------------------------------------------- #


def test_unsupported_capability_reports_no_further_axes():
    """A capability the backend does not implement must not claim to be unbuilt -
    'no' and '-' say different things and only one of them is true here."""
    spec = CapabilitySpec(support=Support.NOT_IMPLEMENTED,
                          configured=lambda cfg: True, built=lambda cfg: True)
    state = spec.evaluate(Capability.MORPHODYNAMICS, cfg=object())
    assert state.implemented is Support.NOT_IMPLEMENTED
    assert state.configured is None and state.built is None and state.run is None
    assert state.row().split()[1:] == ["no", "-", "-", "-"]


def test_not_applicable_is_distinct_from_not_implemented():
    """OpenFOAM has no depth-averaged mode (n/a); it has no morphodynamics *yet* (no).
    Reporting both as 'no' would tell a user to wait for something that is never
    coming."""
    from axqua.solvers.openfoam.spec import SPEC as openfoam
    from axqua.solvers.telemac.spec import SPEC as telemac

    assert openfoam.support(Capability.STEADY2D) is Support.NOT_APPLICABLE
    assert openfoam.support(Capability.MORPHODYNAMICS) is Support.NOT_IMPLEMENTED
    assert telemac.support(Capability.FREE_SURFACE_3D) is Support.NOT_APPLICABLE
    assert telemac.support(Capability.MORPHODYNAMICS) is Support.SUPPORTED


def test_built_and_run_are_blank_when_not_configured():
    """Asking whether an unrequested capability is built is noise, not information."""
    spec = CapabilitySpec(support=Support.SUPPORTED,
                          configured=lambda cfg: False,
                          built=lambda cfg: True, run=lambda cfg: True)
    state = spec.evaluate(Capability.UNSTEADY2D, cfg=object())
    assert state.configured is False
    assert state.built is None and state.run is None


def test_run_cannot_be_true_without_built():
    spec = CapabilitySpec(support=Support.SUPPORTED, configured=lambda cfg: True,
                          built=lambda cfg: False, run=lambda cfg: True)
    state = spec.evaluate(Capability.STEADY2D, cfg=object())
    assert state.built is False and state.run is False


def test_a_failing_predicate_degrades_to_unknown_rather_than_raising():
    """A status report must never be the thing that breaks a session."""
    def boom(cfg):
        raise OSError("permission denied")

    spec = CapabilitySpec(support=Support.SUPPORTED, configured=boom)
    state = spec.evaluate(Capability.STEADY2D, cfg=object())
    assert state.configured is None


def test_available_means_implemented_and_asked_for():
    supported = CapabilityState(Capability.STEADY2D, Support.SUPPORTED, configured=True)
    unasked = CapabilityState(Capability.STEADY2D, Support.SUPPORTED, configured=False)
    missing = CapabilityState(Capability.MORPHODYNAMICS, Support.NOT_IMPLEMENTED)
    assert supported.available
    assert not unasked.available
    assert not missing.available


# --------------------------------------------------------------------------- #
# marker files
# --------------------------------------------------------------------------- #


def _status(**kw) -> SolverStatus:
    return SolverStatus(
        name="telemac", enabled=True, version="0.1.0", case_name="demo",
        generated="2026-08-12T12:00:00", env_detail="pysource.sh",
        capabilities=[
            CapabilityState(Capability.STEADY2D, Support.SUPPORTED, True, True, True),
            CapabilityState(Capability.UNSTEADY2D, Support.SUPPORTED, False),
            CapabilityState(Capability.FREE_SURFACE_3D, Support.NOT_APPLICABLE),
        ], **kw)


def test_marker_name_encodes_solver_and_state():
    assert _status().marker_name == "MODEL=TELEMAC_ENABLED"
    disabled = _status()
    disabled.enabled = False
    assert disabled.marker_name == "MODEL=TELEMAC_DISABLED"


def test_marker_round_trips(tmp_path):
    """The file is a tool contract, so parsing it back is tested, not assumed."""
    path = _status().write(tmp_path)
    assert path.name == "MODEL=TELEMAC_ENABLED"
    back = read_marker(path)
    assert back.name == "telemac"
    assert back.enabled is True
    assert back.case_name == "demo"
    assert back.env_ok is None                      # "not checked" survives as unknown
    assert [c.capability for c in back.capabilities] == [
        Capability.STEADY2D, Capability.UNSTEADY2D, Capability.FREE_SURFACE_3D]
    first = back.capabilities[0]
    assert (first.implemented, first.configured, first.built, first.run) == (
        Support.SUPPORTED, True, True, True)
    assert back.capabilities[2].configured is None   # '-' is not False


def test_writing_removes_the_contradictory_stale_marker(tmp_path):
    """Enabling a solver flips the filename; the old one must not survive beside it."""
    disabled = _status()
    disabled.enabled = False
    disabled.write(tmp_path)
    assert (tmp_path / "MODEL=TELEMAC_DISABLED").exists()

    _status().write(tmp_path)
    assert (tmp_path / "MODEL=TELEMAC_ENABLED").exists()
    assert not (tmp_path / "MODEL=TELEMAC_DISABLED").exists()


def test_marker_body_is_comment_and_key_value_only():
    """The grammar a downstream tool relies on: '#' comments, 'key = value', then a
    fixed-width table after [capabilities]."""
    body = _status().render().splitlines()
    assert body[0] == "MODEL=TELEMAC_ENABLED"
    header = [ln for ln in body if "=" in ln and not ln.startswith("#")]
    assert any(ln.startswith("solver      = telemac") for ln in header)
    assert "[capabilities]" in body
    table = body[body.index("[capabilities]") + 1:]
    for line in table:
        if line and not line.startswith("#"):
            assert len(line.split()) == 5


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_both_builtin_backends_are_registered():
    names = registry.names()
    assert "telemac" in names and "openfoam" in names


def test_supporting_answers_which_code_can_do_this():
    morph = [s.name for s in registry.supporting(Capability.MORPHODYNAMICS)]
    vof = [s.name for s in registry.supporting(Capability.FREE_SURFACE_3D)]
    assert morph == ["telemac"]
    assert vof == ["openfoam"]


def test_registering_a_third_party_backend():
    """The extension seam: a backend is a BackendSpec, registered by name."""
    spec = BackendSpec(name="demo-code", title="Demo", config_key="demo",
                       capabilities={Capability.STEADY2D:
                                     CapabilitySpec(Support.SUPPORTED)})
    try:
        registry.register(spec)
        assert registry.get("demo-code") is spec
        assert "demo-code" in [s.name for s in
                               registry.supporting(Capability.STEADY2D)]
    finally:
        registry._REGISTRY.pop("demo-code", None)


def test_unknown_backend_names_the_registered_ones():
    with pytest.raises(KeyError, match="telemac"):
        registry.get("nonexistent")


def test_loading_an_unimplemented_backend_says_so():
    spec = BackendSpec(name="paper-only", title="x", config_key="x")
    with pytest.raises(NotImplementedError, match="status reporting only"):
        spec.load()


def test_listing_capabilities_imports_nothing_heavy():
    """The constraint that makes a QGIS plugin possible: enumerating what a case can
    do must not drag in the scientific stack or a solver. Asserted in a subprocess so
    the rest of the suite's imports cannot mask it."""
    code = textwrap.dedent("""
        import sys
        import axqua
        from axqua.core import registry
        from axqua.core.capabilities import Capability
        specs = registry.backends()
        assert {s.name for s in specs} >= {"telemac", "openfoam"}
        heavy = sorted(m for m in
                       ("numpy", "scipy", "pandas", "geopandas", "rasterio",
                        "shapely", "matplotlib", "gmsh", "openpyxl")
                       if m in sys.modules)
        print(",".join(heavy))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "", f"heavy imports leaked in: {out.stdout.strip()}"


# --------------------------------------------------------------------------- #
# the lazy public API
# --------------------------------------------------------------------------- #


def test_every_public_name_resolves():
    """Lazy attribute access means a typo in the export map is invisible until
    someone imports that one name, so every advertised name is resolved here."""
    import axqua

    for name in axqua.__all__:
        assert getattr(axqua, name) is not None, name


def test_all_matches_the_export_map():
    """__all__ is written out by hand for the linters; this stops it drifting."""
    import axqua

    expected = set(axqua._NAME_TO_MODULE) | set(axqua._SUBMODULES)
    assert set(axqua.__all__) - {"__version__"} == expected


def test_unknown_attribute_still_raises_attribute_error():
    import axqua

    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        axqua.nope


# --------------------------------------------------------------------------- #
# end to end on a real (minimal) config
# --------------------------------------------------------------------------- #


MINIMAL = """
project:
  name: status-demo
  crs_epsg: 25832
telemac:
  pysource: {pysource}
geodata:
  dem_initial: dem.tif
  boundary: roi.gpkg
boundaries:
  liquid_boundaries: liquid.gpkg
  prescribed_flowrate: 2.4
  prescribed_elevation: 100.0
"""


def _write_case(tmp_path, *, openfoam: bool) -> "object":
    from axqua.config import load_config

    (tmp_path / "pysource.sh").write_text("# stub\n")
    text = MINIMAL.format(pysource="pysource.sh")
    if openfoam:
        text += "openfoam:\n  bashrc: bashrc.sh\n"
        (tmp_path / "bashrc.sh").write_text("# stub\n")
    (tmp_path / "case-config.yml").write_text(text)
    return load_config(tmp_path / "case-config.yml")


def test_openfoam_is_disabled_when_the_case_does_not_declare_it(tmp_path):
    """ENABLED reflects the CASE, so the filename means the same on any machine."""
    cfg = _write_case(tmp_path, openfoam=False)
    status = CaseStatus.collect(cfg)
    assert status.solver("telemac").enabled is True
    assert status.solver("openfoam").enabled is False

    written = {p.name for p in status.write_markers()}
    assert written == {"MODEL=TELEMAC_ENABLED", "MODEL=OPENFOAM_DISABLED"}


def test_declaring_openfoam_flips_the_marker(tmp_path):
    cfg = _write_case(tmp_path, openfoam=True)
    status = CaseStatus.collect(cfg)
    assert status.solver("openfoam").enabled is True
    assert status.solver("openfoam").capability(
        Capability.FREE_SURFACE_3D).configured is True
    # nothing is built yet in a bare case
    assert status.solver("openfoam").capability(Capability.FREE_SURFACE_3D).built is False


def test_a_fresh_case_reports_steady2d_configured_but_unbuilt(tmp_path):
    cfg = _write_case(tmp_path, openfoam=False)
    steady = CaseStatus.collect(cfg).solver("telemac").capability(Capability.STEADY2D)
    assert steady.implemented is Support.SUPPORTED
    assert steady.configured is True
    assert steady.built is False and steady.run is False


# --------------------------------------------------------------------------- #
# compatibility shims for the moved modules
# --------------------------------------------------------------------------- #


MOVED_MODULES = [
    ("axqua.mesh", "axqua.solvers.telemac.mesh"),
    ("axqua.steering", "axqua.solvers.telemac.steering"),
    ("axqua.boundary", "axqua.solvers.telemac.boundary"),
    ("axqua.pipeline", "axqua.solvers.telemac.pipeline"),
    ("axqua.threed", "axqua.solvers.telemac.threed"),
    ("axqua.unsteady", "axqua.solvers.telemac.unsteady"),
    ("axqua.sortie", "axqua.solvers.telemac.sortie"),
    ("axqua.wetting", "axqua.solvers.telemac.wetting"),
    ("axqua.sections", "axqua.solvers.telemac.sections"),
    ("axqua.fortran", "axqua.solvers.telemac.fortran"),
    ("axqua.gainlose", "axqua.solvers.telemac.gainlose"),
    ("axqua.watertable", "axqua.solvers.telemac.watertable"),
    ("axqua.mesh_quality", "axqua.solvers.telemac.mesh_quality"),
    ("axqua.flux_convergence", "axqua.solvers.telemac.flux_convergence"),
    ("axqua.selafin", "axqua.core.selafin"),
    ("axqua.openfoam", "axqua.solvers.openfoam"),
]


@pytest.mark.parametrize("old,new", MOVED_MODULES, ids=[o for o, _ in MOVED_MODULES])
def test_the_old_import_path_is_the_same_module_object(old, new):
    """Case scripts written before the backends were split out are sitting in users'
    working trees. The shims alias through ``sys.modules`` rather than re-exporting,
    so there is exactly one module and no second surface to keep in step - which also
    means private names and identity checks survive."""
    import importlib

    assert importlib.import_module(old) is importlib.import_module(new)


def test_private_names_survive_the_shim():
    """A re-export shim would have dropped these silently; tests import them."""
    from axqua.mesh import _classify_zone, _parse_decimal, _ZONE_PRIORITY

    assert _parse_decimal("0,5") == 0.5
    assert _classify_zone("Main channel") == "channel"
    assert _ZONE_PRIORITY["channel"] == 1


def test_nothing_in_core_imports_a_solver():
    """The rule the layering exists to enforce: backends depend on the core, never
    the other way round. Checked by reading the source, so it holds even for imports
    that only happen inside a function."""
    import pathlib
    import re

    core = pathlib.Path(__file__).resolve().parent.parent / "src" / "axqua" / "core"
    offenders = []
    for path in core.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"^\s*(from|import)\s+axqua\.solvers\b", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "core imports a solver backend:\n  " + "\n  ".join(offenders)


def test_openfoam_does_not_import_the_telemac_backend():
    """The two backends are siblings. OpenFOAM is *hotstarted* from a TELEMAC result,
    but it reads that through the shared SERAFIN codec in the core, not by reaching
    into the other backend."""
    import pathlib
    import re

    root = (pathlib.Path(__file__).resolve().parent.parent / "src" / "axqua"
            / "solvers" / "openfoam")
    offenders = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"axqua\.solvers\.telemac\b", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, ("the OpenFOAM backend reaches into TELEMAC:\n  "
                           + "\n  ".join(offenders))
