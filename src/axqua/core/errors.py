"""What axqua raises, and why the type matters more than the message.

Until now a failure surfaced as whatever the underlying library threw - a
``ValueError`` from a config check, a ``KeyError`` from a missing gpkg column, an
``OSError`` from a solver that was not installed. That is fine at a terminal, where a
person reads the sentence and knows what to do. It stops being fine the moment
something *else* has to act on the failure:

* a job record has to store a failure that survives serialisation, so "what went
  wrong" cannot be a formatted string that only means something to the reader;
* a plugin has to show an actionable message next to the field that caused it, which
  means it needs the field name as data, not embedded in prose;
* a batch of runs has to decide whether a failure is worth retrying - a solver that
  died mid-run might be; a config that names a missing file never will be.

So every error carries a **stable machine-readable code**, an optional **subject**
(the config key, layer or path the failure is about) and an optional **remedy** (the
one thing to try). The message is still a sentence, because a person reads it first.

Deliberately shallow
--------------------
Five categories, one level of nesting. The temptation with an exception hierarchy is
to mirror the module layout, which produces thirty classes that callers then catch by
their common base anyway. These five are the distinctions that actually change what a
caller does:

``ConfigError``       the case description is wrong        -> fix the YAML
``GeodataError``      an input layer or raster is wrong    -> fix the data
``EnvironmentError``  the solver cannot be reached         -> fix the install
``SolverError``       the solver ran and failed            -> read the listing
``MeshError``         the mesh could not be built or used  -> change resolution

Nothing here is required to raise these: existing ``ValueError``s keep working, and
this is additive. Use it where the failure has an obvious remedy worth carrying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AxquaError(Exception):
    """Base class: a failure axqua can describe rather than merely report.

    ``code`` is the contract. It is a stable, lower-case, dotted string that a caller
    may branch on and a UI may translate; it must not change when the wording does.
    """

    code = "axqua.error"

    def __init__(self, message: str, *, subject: str | None = None,
                 remedy: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.subject = subject
        self.remedy = remedy
        self.details = details

    def __str__(self) -> str:
        parts = [self.message]
        if self.remedy:
            parts.append(self.remedy)
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form, for a job record or a plugin.

        Everything is a string or a plain container: a failure that cannot be written
        to ``status.json`` is a failure that gets lost.
        """
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.subject:
            out["subject"] = self.subject
        if self.remedy:
            out["remedy"] = self.remedy
        if self.details:
            out["details"] = {k: _plain(v) for k, v in self.details.items()}
        return out


def _plain(value: Any) -> Any:
    """Coerce to something ``json.dumps`` will accept."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


class ConfigError(AxquaError):
    """The case description is wrong: a missing key, a bad value, a broken path.

    ``subject`` is the dotted config key (``boundaries.outflow_condition``), which is
    what lets a form-based editor put the message on the right field instead of at the
    top of the page.
    """

    code = "axqua.config"


class GeodataError(AxquaError):
    """An input layer or raster cannot be used: absent, wrong CRS, missing column."""

    code = "axqua.geodata"


class EnvironmentError(AxquaError):  # noqa: A001 - shadows the builtin knowingly
    """The solver cannot be reached: no setup script, wrong shell, absent install.

    Shadows the builtin inside this module's namespace on purpose. Callers write
    ``errors.EnvironmentError``, where the qualification says which one is meant, and
    the builtin remains reachable as ``builtins.EnvironmentError`` for the rare code
    that wants it (it is an alias of ``OSError``, which axqua never raises
    deliberately).
    """

    code = "axqua.environment"


class SolverError(AxquaError):
    """The solver ran and failed. ``listing`` points at what to read."""

    code = "axqua.solver"


class MeshError(AxquaError):
    """A mesh could not be built, or is unusable once built."""

    code = "axqua.mesh"


@dataclass
class ErrorRecord:
    """A failure as stored rather than raised - what a job record keeps.

    Any exception can become one, so a job that died on a ``KeyError`` deep in a
    third-party library still records something structured. Unknown types get the
    generic code, which is honest: it says "this was not a failure axqua
    anticipated" rather than mislabelling it.
    """

    code: str
    message: str
    subject: str | None = None
    remedy: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorRecord":
        if isinstance(exc, AxquaError):
            data = exc.as_dict()
            return cls(code=data["code"], message=data["message"],
                       subject=data.get("subject"), remedy=data.get("remedy"),
                       details=data.get("details", {}))
        return cls(code="axqua.unexpected", message=str(exc),
                   details={"type": type(exc).__name__})

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        for key in ("subject", "remedy"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.details:
            out["details"] = self.details
        return out
