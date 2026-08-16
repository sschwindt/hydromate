"""What a case can do: the capability vocabulary and the ``MODEL=`` marker files.

A hydromate case folder used to say nothing about itself. You could not tell, without
reading the config and poking at ``hydromate-case/``, which solvers were set up, which
features hydromate actually implements for them, or how far this particular case had
got. This module makes that explicit and writes it next to ``case-config.yml``.

Three axes, deliberately kept apart
-----------------------------------
Conflating these is the mistake this module exists to avoid, because each answers a
different user question and each has a different fix:

``implemented``
    Does hydromate support this capability **for this solver at all**? A static
    property of the code, from the backend's own matrix. Three values, not two:
    OpenFOAM's ``steady2d`` is *not applicable* (a VOF free-surface model is
    inherently 3D and transient), while its ``morphodynamics`` is *not implemented*
    (it could be; it isn't yet). Telling a user "no" for both would be misleading -
    one is a gap in hydromate, the other is a category error.

``configured``
    Does **this case** ask for it? Derived from the config alone: a varying inflow
    series implies unsteady, ``morphodynamics.enabled`` implies GAIA, calibration
    parameters imply a BAL run.

``built`` / ``run``
    Do the **artifacts** exist on disk? A pure path check per backend, so it stays
    cheap and never launches anything.

Why the files are generated and gitignored
------------------------------------------
The marker's *name* (``MODEL=TELEMAC_ENABLED``) reflects the **case** - whether the
config declares that solver - so it is portable and means the same on any machine.
Its *body* reports this machine and this working tree: whether the solver environment
resolves, and what has been built. That is local state, exactly like
``hydromate-case/``, so the files are regenerated rather than tracked; a tracked copy
would churn on every build and would be a confident lie in a fresh clone.
``cases/case-template/`` ships one committed example so the format stays documented.

Import weight
-------------
**Standard library only, and it must stay that way.** Enumerating what a case can do
has to work in a process that has never imported gmsh, rasterio or OpenFOAM - a future
QGIS plugin will do exactly that. Everything here works on paths and plain attribute
access.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

MARKER_PREFIX = "MODEL="
_HEADER_RULE = "# " + "-" * 74


class Capability(str, Enum):
    """The shared vocabulary. One name per modelling capability, across all solvers.

    Shared on purpose: "does this reach have an unsteady case" is the same question
    whichever code answers it, and a per-solver vocabulary would make the status
    table unreadable and the registry useless.
    """

    STEADY2D = "steady2d"
    UNSTEADY2D = "unsteady2d"
    STEADY3D = "steady3d"
    UNSTEADY3D = "unsteady3d"
    FREE_SURFACE_3D = "free_surface_3d"      # two-phase VOF with a resolved surface
    MORPHODYNAMICS = "morphodynamics"
    CALIBRATION = "calibration"              # surrogate-assisted Bayesian (HydroBayesCal)
    MESH_CONVERGENCE = "mesh_convergence"    # horizontal grid independence
    VERTICAL_CONVERGENCE = "vertical_convergence"
    GAIN_LOSE = "gain_lose"                  # losing/gaining reach exchange

    def __str__(self) -> str:                # so f-strings render the value
        return self.value


class Support(str, Enum):
    """Whether a backend implements a capability, and if not, why not."""

    SUPPORTED = "yes"
    NOT_IMPLEMENTED = "no"
    NOT_APPLICABLE = "n/a"

    def __str__(self) -> str:
        return self.value


# render order of the status table; stable so diffs between runs are readable
CAPABILITY_ORDER: tuple[Capability, ...] = (
    Capability.STEADY2D,
    Capability.UNSTEADY2D,
    Capability.STEADY3D,
    Capability.UNSTEADY3D,
    Capability.FREE_SURFACE_3D,
    Capability.MORPHODYNAMICS,
    Capability.GAIN_LOSE,
    Capability.MESH_CONVERGENCE,
    Capability.VERTICAL_CONVERGENCE,
    Capability.CALIBRATION,
)


def _flag(value: bool | None) -> str:
    """Render a tri-state cell: yes / no / '-' where the question does not arise."""
    return "-" if value is None else ("yes" if value else "no")


def _parse_flag(text: str) -> bool | None:
    return None if text == "-" else text == "yes"


@dataclass
class CapabilityState:
    """One capability's state on all three axes for one solver."""

    capability: Capability
    implemented: Support
    configured: bool | None = None
    built: bool | None = None
    run: bool | None = None

    @property
    def available(self) -> bool:
        """Ready to use: implemented here and asked for by this case."""
        return self.implemented is Support.SUPPORTED and bool(self.configured)

    def row(self) -> str:
        return (f"{self.capability.value:<22}{self.implemented.value:<13}"
                f"{_flag(self.configured):<12}{_flag(self.built):<7}"
                f"{_flag(self.run)}")

    def as_dict(self) -> dict:
        """JSON form, for ``hydromate case-status --json``.

        The three axes stay separate and the unknown ones stay ``null``. A GUI needs
        that distinction: ``n/a`` means hide the tab (a category error for this solver),
        ``no`` means show it disabled with a reason (hydromate has not built it yet),
        and ``null`` means "could not tell" - which is never the same as "no".
        """
        return {
            "capability": self.capability.value,
            "implemented": self.implemented.value,
            "configured": self.configured,
            "built": self.built,
            "run": self.run,
            "available": self.available,
        }


@dataclass
class SolverStatus:
    """Everything the marker file records about one solver."""

    name: str
    enabled: bool
    capabilities: list[CapabilityState] = field(default_factory=list)
    env_ok: bool | None = None          # None = not checked (checking can be slow)
    env_detail: str = ""
    case_name: str = ""
    generated: str = ""
    version: str = ""

    @property
    def marker_name(self) -> str:
        state = "ENABLED" if self.enabled else "DISABLED"
        return f"{MARKER_PREFIX}{self.name.upper()}_{state}"

    def capability(self, cap: Capability) -> CapabilityState | None:
        for state in self.capabilities:
            if state.capability is cap:
                return state
        return None

    def as_dict(self) -> dict:
        return {
            "solver": self.name,
            "enabled": self.enabled,
            "env_ok": self.env_ok,
            "env_detail": self.env_detail,
            "case": self.case_name,
            "generated": self.generated,
            "version": self.version,
            "capabilities": [state.as_dict() for state in self.capabilities],
        }

    def render(self) -> str:
        """The marker file body. Strictly parseable: ``#`` comments, ``key = value``
        header, then a fixed-width table after ``[capabilities]``."""
        env = "not checked" if self.env_ok is None else ("ok" if self.env_ok else "unavailable")
        lines = [
            f"{MARKER_PREFIX}{self.name.upper()}_"
            f"{'ENABLED' if self.enabled else 'DISABLED'}",
            f"# generated by hydromate {self.version} - regenerated by every build "
            "and by `hydromate status`; do not edit",
            _HEADER_RULE,
            f"solver      = {self.name}",
            f"enabled     = {'yes' if self.enabled else 'no'}",
            f"env         = {env}",
            f"env_detail  = {self.env_detail}",
            f"case        = {self.case_name}",
            f"generated   = {self.generated}",
            "",
            "[capabilities]",
            f"# {'name':<20}{'implemented':<13}{'configured':<12}{'built':<7}run",
        ]
        lines += [state.row() for state in self.capabilities]
        lines.append("")
        return "\n".join(lines)

    def write(self, case_dir: str | Path) -> Path:
        """Write the marker, removing any stale one for this solver first.

        The removal matters: enabling a solver flips the *filename*, so without it a
        case would keep a contradictory ``MODEL=OPENFOAM_DISABLED`` beside its new
        ``MODEL=OPENFOAM_ENABLED``.
        """
        case_dir = Path(case_dir)
        for stale in case_dir.glob(f"{MARKER_PREFIX}{self.name.upper()}_*"):
            stale.unlink()
        path = case_dir / self.marker_name
        path.write_text(self.render())
        return path


_KEY_RE = re.compile(r"^([a-z_]+)\s*=\s*(.*)$")


def read_marker(path: str | Path) -> SolverStatus:
    """Parse a marker file back into a :class:`SolverStatus`.

    Round-tripping is not decoration: it is the contract a QGIS plugin (or any other
    tool) reads, so it is tested rather than assumed.
    """
    path = Path(path)
    header: dict[str, str] = {}
    caps: list[CapabilityState] = []
    in_table = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[capabilities]":
            in_table = True
            continue
        if not in_table:
            match = _KEY_RE.match(line)
            if match:
                header[match.group(1)] = match.group(2).strip()
            continue
        parts = line.split()
        if len(parts) != 5:
            continue
        caps.append(CapabilityState(
            capability=Capability(parts[0]),
            implemented=Support(parts[1]),
            configured=_parse_flag(parts[2]),
            built=_parse_flag(parts[3]),
            run=_parse_flag(parts[4]),
        ))
    env = header.get("env", "not checked")
    return SolverStatus(
        name=header.get("solver", path.name),
        enabled=header.get("enabled") == "yes",
        capabilities=caps,
        env_ok=None if env == "not checked" else env == "ok",
        env_detail=header.get("env_detail", ""),
        case_name=header.get("case", ""),
        generated=header.get("generated", ""),
    )


@dataclass
class CaseStatus:
    """The whole case: one :class:`SolverStatus` per registered backend."""

    case_dir: Path
    solvers: list[SolverStatus] = field(default_factory=list)

    @classmethod
    def collect(cls, cfg, *, check_env: bool = False) -> "CaseStatus":
        """Build the status for *cfg* from every registered backend.

        *check_env* is off by default because probing an environment means spawning a
        shell per solver; the CLI turns it on, a bulk listing leaves it off.
        """
        from hydromate import __version__
        from hydromate.core.registry import backends

        now = _dt.datetime.now().replace(microsecond=0).isoformat()
        solvers = []
        for spec in backends():
            status = spec.status(cfg, check_env=check_env)
            status.case_name = getattr(cfg, "name", "")
            status.generated = now
            status.version = __version__
            solvers.append(status)
        return cls(case_dir=Path(cfg.config_dir), solvers=solvers)

    def solver(self, name: str) -> SolverStatus | None:
        for status in self.solvers:
            if status.name == name:
                return status
        return None

    def write_markers(self) -> list[Path]:
        return [status.write(self.case_dir) for status in self.solvers]

    def as_dict(self) -> dict:
        """The whole case as JSON. **This is the plugin's tab-generation contract.**

        The QGIS plugin builds its tab set from this rather than hardcoding one, so
        adding a capability - or a whole solver - to hydromate surfaces in the plugin
        with no plugin change. It must stay standard-library-only for the same reason
        the rest of this module does: a plugin asking "what can this case do?" has to
        get an answer without importing gmsh.
        """
        return {
            "case_dir": str(self.case_dir),
            "solvers": [status.as_dict() for status in self.solvers],
        }

    def render(self) -> str:
        return "\n".join(status.render() for status in self.solvers)

    def write_and_summarise(self) -> list[str]:
        self.write_markers()
        return self.summary()

    def summary(self) -> list[str]:
        """Short console form: one line per solver, then its available capabilities."""
        out: list[str] = []
        for status in self.solvers:
            env = ("" if status.env_ok is None
                   else f", environment {'ok' if status.env_ok else 'UNAVAILABLE'}")
            out.append(f"{status.name}: {'enabled' if status.enabled else 'disabled'}"
                       f"{env}")
            for state in status.capabilities:
                if state.implemented is not Support.SUPPORTED:
                    continue
                marks = []
                if state.configured:
                    marks.append("configured")
                if state.built:
                    marks.append("built")
                if state.run:
                    marks.append("run")
                out.append(f"    {state.capability.value:<22}"
                           f"{', '.join(marks) if marks else 'not set up'}")
        return out


def refresh_markers(cfg) -> list[Path]:
    """Rewrite a case's marker files. Never raises.

    Called at the end of every build, so the files describe the case as it is now
    rather than as it was when someone last ran ``hydromate status``. A build that
    succeeded must not be reported as failed because a status file could not be
    written, hence the blanket catch.
    """
    try:
        return CaseStatus.collect(cfg).write_markers()
    except Exception:  # noqa: BLE001 - status must never break a build
        import logging

        logging.getLogger("hydromate").debug("could not refresh MODEL= markers",
                                             exc_info=True)
        return []
