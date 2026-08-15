"""The solver registry: how hydromate finds, describes and loads a simulation backend.

hydromate used to reach for TELEMAC directly - ``pipeline.run`` imported
``steering``, ``selafin`` and the rest by name, so adding a second code meant copying
the pipeline rather than plugging into it. This module is the seam that replaces that.

Two objects, and the split between them is the whole design
-----------------------------------------------------------
:class:`BackendSpec`
    **Metadata**, and nothing else: the backend's name, which capabilities it
    implements, which config block it owns, and where to find each of the
    capability-to-artifact path checks. Import-light by construction - a spec module
    may use the standard library and read attributes off a ``Config``, and that is
    all.

:class:`SolverBackend`
    The **implementation**: build a case, run it, report on it. Loaded only when
    something actually needs to do work.

Keeping them apart is what lets ``hydromate status`` - and, later, a QGIS plugin
running inside the QGIS Python process - answer "what can this case do?" without
importing gmsh, rasterio, or OpenFOAM's Python. That question gets asked far more
often than "now go and mesh it", and it must stay cheap.

Adding a backend
----------------
Third-party backends are discovered through the ``hydromate.solvers`` entry-point
group, pointing at a module attribute that is a :class:`BackendSpec`::

    [project.entry-points."hydromate.solvers"]
    basement = "hydromate_basement.spec:SPEC"

The built-ins register through the same mechanism, with a hard-coded fallback list so
a source checkout that has not been re-installed still works. "Add your own" and "use
ours" are therefore the same path, which is the only way the extension point stays
honest.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, runtime_checkable

from hydromate.core.capabilities import (
    CAPABILITY_ORDER, Capability, CapabilityState, SolverStatus, Support,
)

log = logging.getLogger("hydromate")

ENTRY_POINT_GROUP = "hydromate.solvers"

# Built-in specs, as "module:attribute". Duplicated deliberately rather than relying
# solely on entry points: a `pip install -e .` that predates this group would
# otherwise silently register nothing, and a source checkout is the normal way this
# package is used.
_BUILTIN_SPECS: tuple[str, ...] = (
    "hydromate.solvers.telemac.spec:SPEC",
    "hydromate.solvers.openfoam.spec:SPEC",
)

# capability -> (configured?, built?, run?) predicates, all taking the Config
Predicate = Callable[[object], bool]


@dataclass(frozen=True)
class CapabilitySpec:
    """How to answer the three axes for one capability, without running anything."""

    support: Support
    configured: Predicate | None = None
    built: Predicate | None = None
    run: Predicate | None = None

    def evaluate(self, capability: Capability, cfg) -> CapabilityState:
        """Fill in the axes that apply. Anything the backend cannot answer is left
        ``None`` and renders as ``-`` rather than as a confident ``no``."""
        if self.support is not Support.SUPPORTED:
            return CapabilityState(capability=capability, implemented=self.support)
        configured = _safe(self.configured, cfg)
        state = CapabilityState(capability=capability, implemented=self.support,
                                configured=configured)
        # Asking whether something is built when the case never asked for it is
        # noise, so those cells stay blank.
        if configured:
            state.built = _safe(self.built, cfg)
            state.run = _safe(self.run, cfg) if state.built else False
        return state


def _safe(predicate: Predicate | None, cfg) -> bool | None:
    """Run a predicate, treating any failure as "cannot tell".

    A status report must never be the thing that breaks a session: a half-written
    case, a permissions error or a path that moved should downgrade one cell to
    ``-``, not raise out of ``hydromate status``.
    """
    if predicate is None:
        return None
    try:
        return bool(predicate(cfg))
    except Exception as exc:  # noqa: BLE001 - reporting must not raise
        log.debug("capability predicate failed: %s", exc)
        return None


@dataclass(frozen=True)
class BackendSpec:
    """Import-light description of a solver backend."""

    name: str                       # "telemac", "openfoam"
    title: str                      # human-readable, e.g. "TELEMAC-2D/3D"
    config_key: str                 # the config attribute holding its block
    capabilities: Mapping[Capability, CapabilitySpec] = field(default_factory=dict)
    # "module:attribute" of the SolverBackend implementation, imported on demand
    implementation: str = ""
    # is this solver declared by the case at all? (defaults to "the block exists")
    enabled: Predicate | None = None
    # short description of the environment this backend needs, for the marker body
    environment: Callable[[object], str] | None = None

    def support(self, capability: Capability) -> Support:
        spec = self.capabilities.get(capability)
        return spec.support if spec else Support.NOT_IMPLEMENTED

    def is_enabled(self, cfg) -> bool:
        return bool(_safe(self.enabled, cfg)) if self.enabled else True

    def status(self, cfg, *, check_env: bool = False) -> SolverStatus:
        """Evaluate every capability for *cfg*."""
        states = [
            self.capabilities.get(cap, CapabilitySpec(Support.NOT_IMPLEMENTED))
            .evaluate(cap, cfg)
            for cap in CAPABILITY_ORDER
        ]
        detail = ""
        if self.environment is not None:
            detail = _safe_str(self.environment, cfg)
        env_ok = None
        if check_env:
            env_ok = self.check_environment(cfg)
        return SolverStatus(name=self.name, enabled=self.is_enabled(cfg),
                            capabilities=states, env_ok=env_ok, env_detail=detail)

    def check_environment(self, cfg) -> bool | None:
        """Whether this machine can actually run the solver.

        Probes the configured environment (:mod:`hydromate.core.environment`) and
        checks it yields that solver's sentinel variables - so a wrong setup script is
        caught here rather than hours into a job. Only ever called when a caller asks
        for it (``hydromate status --check-env``), because it spawns a shell.
        """
        try:
            from hydromate.core.environment import SolverEnvironment

            block = getattr(getattr(cfg, self.config_key, None), "environment", None)
            legacy = (getattr(cfg.telemac, "pysource", None) if self.name == "telemac"
                      else getattr(cfg.openfoam, "bashrc", None))
            env = SolverEnvironment.from_config(block, legacy_script=legacy)
            if env.setup_script is None:
                return None                      # nothing configured: not an answer
            return bool(env.validate(self.name))
        except Exception as exc:  # noqa: BLE001 - an absent solver is a normal answer
            log.debug("%s environment check failed: %s", self.name, exc)
            return False

    def load(self) -> "SolverBackend":
        """Import and return the implementation. The only heavyweight call here."""
        if not self.implementation:
            raise NotImplementedError(
                f"the {self.name!r} backend is registered for status reporting only; "
                "it has no implementation module to build or run with.")
        module_name, _, attribute = self.implementation.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attribute) if attribute else module


def _safe_str(fn, cfg) -> str:
    try:
        return str(fn(cfg) or "")
    except Exception:  # noqa: BLE001 - cosmetic field
        return ""


@runtime_checkable
class SolverBackend(Protocol):
    """What an implementation must provide to be driven by hydromate.

    Intentionally small. Everything a backend needs that is *not* solver-specific -
    the geodata, the boundary conditions, the ground truth, the convergence maths -
    comes from :mod:`hydromate.core`, so a new backend implements the parts that are
    genuinely its own and inherits the rest.
    """

    name: str

    def check_environment(self, cfg) -> bool:
        """Can this machine run the solver? Must not raise."""
        ...

    def build(self, cfg, capability: Capability, **kwargs):
        """Assemble the case for *capability*; return the produced artifacts."""
        ...

    def run(self, cfg, capability: Capability, **kwargs):
        """Launch the solver on an already-built case."""
        ...

    def study(self, cfg, capability: Capability, **kwargs):
        """Run a multi-run investigation: convergence ladder, calibration campaign.

        A third verb rather than folding these into :meth:`run`, because a study is
        genuinely neither a build nor a run - it repeatedly does both, decides when to
        stop, and reports on the sequence. Folding it into ``run`` would make every
        caller inspect the capability to know what it just started.
        """
        ...

    def postprocess(self, cfg, ctx):
        """Turn raw solver output into results: reconstruction, reports, conversions.

        Part of the **runner** lifecycle, deliberately (plan §13): QGIS must never do
        heavy OpenFOAM postprocessing, so the expensive step happens where the solver
        ran, before the job reports COMPLETED.
        """
        ...

    def extract_objective(self, cfg, ctx) -> float | None:
        """The single number a calibration is minimising, if this job produced one."""
        ...

    def export_qgis_results(self, cfg, ctx):
        """Write ``results/qgis/`` and describe it in the result manifest.

        **Never imports PyQGIS.** It creates files and says what they are; the plugin
        loads and styles them (plan §14). That separation is what lets the runner work
        on a headless cluster node.
        """
        ...


class BaseBackend:
    """Optional base supplying no-op defaults for the parts a backend does not have.

    :class:`SolverBackend` has six methods, and a third-party backend that only builds
    cases should not have to write four stubs to satisfy a ``runtime_checkable``
    protocol. Subclassing is not required - the protocol is structural - but it removes
    the boilerplate and documents which parts are genuinely optional.
    """

    name: str = ""

    def check_environment(self, cfg) -> bool:
        return True

    def build(self, cfg, capability: Capability, **kwargs):
        raise NotImplementedError(
            f"{self.name or type(self).__name__} cannot build {capability}")

    def run(self, cfg, capability: Capability, **kwargs):
        raise NotImplementedError(
            f"{self.name or type(self).__name__} cannot run {capability}")

    def study(self, cfg, capability: Capability, **kwargs):
        raise NotImplementedError(
            f"{self.name or type(self).__name__} has no study for {capability}")

    def postprocess(self, cfg, ctx):
        return None

    def extract_objective(self, cfg, ctx) -> float | None:
        return None

    def export_qgis_results(self, cfg, ctx):
        return []


# --------------------------------------------------------------------------- #
# registration and lookup
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, BackendSpec] = {}
_DISCOVERED = False


def register(spec: BackendSpec, *, replace: bool = False) -> BackendSpec:
    """Add *spec* to the registry (idempotent unless *replace*)."""
    if spec.name in _REGISTRY and not replace:
        return _REGISTRY[spec.name]
    _REGISTRY[spec.name] = spec
    return spec


def _load_spec(target: str) -> BackendSpec | None:
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - a broken third-party spec must not
        log.warning("could not load solver spec %s: %s", target, exc)  # break hydromate
        return None
    spec = getattr(module, attribute or "SPEC", None)
    if not isinstance(spec, BackendSpec):
        log.warning("%s is not a BackendSpec; ignoring", target)
        return None
    return spec


def discover(*, force: bool = False) -> None:
    """Populate the registry from the built-ins and the entry-point group."""
    global _DISCOVERED
    if _DISCOVERED and not force:
        return
    _DISCOVERED = True
    for target in _BUILTIN_SPECS:
        spec = _load_spec(target)
        if spec is not None:
            register(spec)
    try:
        from importlib.metadata import entry_points

        for entry in entry_points(group=ENTRY_POINT_GROUP):
            spec = _load_spec(entry.value)
            if spec is not None:
                register(spec)
    except Exception as exc:  # noqa: BLE001 - entry points are a bonus, not a need
        log.debug("entry-point discovery skipped: %s", exc)


def backends() -> list[BackendSpec]:
    """Every registered backend spec, in registration order."""
    discover()
    return list(_REGISTRY.values())


def get(name: str) -> BackendSpec:
    """One backend spec by name."""
    discover()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"no solver backend named {name!r}; registered: "
            f"{sorted(_REGISTRY)}"
        ) from None


def names() -> list[str]:
    return [spec.name for spec in backends()]


def supporting(capability: Capability) -> list[BackendSpec]:
    """Backends that implement *capability* - the "which code can do this?" query."""
    return [spec for spec in backends()
            if spec.support(capability) is Support.SUPPORTED]
