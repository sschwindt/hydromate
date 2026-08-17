"""Job identifiers: readable, sortable, collision-safe.

A bare UUID was rejected deliberately. The identifier is a **directory name** and a
**dashboard column**, and ``f835ac7d-3e91-...`` is neither greppable nor meaningful six
months later - the dashboard sketch in the plan truncates it to ``f835ac7``, which is the
problem showing through. So::

    <date>-<case-slug>-<kind-slug>-<6 hex>
    2026-08-14-isar-2025-steady-a3f19c

The date sorts chronologically under a plain ``ls``, the case and kind say what the job
is without opening anything, and the random tail keeps it collision-safe. The tail is
short on purpose: uniqueness is *claimed* by creating the directory with
``mkdir(exist_ok=False)`` (see :mod:`axqua.jobs.paths`), not asserted by entropy, so
six hex digits plus a redraw loop is ample.

Standard library only - this module is imported by the CLI on every job verb.
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets

from axqua.core.errors import ConfigError

#: The one grammar. Anything that does not match is not a job id, which is what lets
#: ``axqua status <arg>`` tell a job id from a case-config path without guessing.
JOB_ID_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"-(?P<slug>[a-z0-9][a-z0-9-]{0,60}?)"
    r"-(?P<tail>[0-9a-f]{6})$"
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_EDGES = re.compile(r"^-+|-+$")

#: How many times :func:`make_job_id` redraws the random tail before giving up. The
#: caller claims the directory; a clash means it lost that race, which at six hex digits
#: needs a deliberate effort to arrange.
MAX_REDRAWS = 8


def slug(text: str, *, limit: int = 32) -> str:
    """Reduce *text* to a lowercase ``[a-z0-9-]`` token fit for a directory name.

    Empty or wholly non-alphanumeric input becomes ``"case"`` rather than an empty
    segment, because an empty segment would make the id ambiguous to parse.
    """
    out = _SLUG_STRIP.sub("-", str(text).lower())
    out = _SLUG_EDGES.sub("", out)[:limit]
    out = _SLUG_EDGES.sub("", out)
    return out or "case"


def make_job_id(case_name: str, kind_slug: str = "", *, when: _dt.date | None = None,
                tail: str | None = None) -> str:
    """Build one job id. *tail* is for tests; production draws it randomly."""
    day = (when or _dt.date.today()).isoformat()
    parts = [day, slug(case_name)]
    if kind_slug:
        parts.append(slug(kind_slug, limit=16))
    parts.append(tail if tail is not None else secrets.token_hex(3))
    return "-".join(parts)


def parse_job_id(job_id: str) -> tuple[_dt.date, str, str]:
    """Split *job_id* into ``(date, slug, tail)``.

    The slug is returned whole - the case and kind halves are not separable after the
    fact, since a case name may itself contain dashes. Nothing needs them apart: the
    authoritative kind lives in ``job.json``.
    """
    match = JOB_ID_RE.match(str(job_id))
    if match is None:
        raise ConfigError(
            f"{job_id!r} is not a job id",
            subject="job_id",
            remedy="Job ids look like 2026-08-14-isar-2025-steady-a3f19c; "
                   "run 'axqua list' to see the known ones.",
        )
    return (
        _dt.date.fromisoformat(match.group("date")),
        match.group("slug"),
        match.group("tail"),
    )


def is_job_id(value: object) -> bool:
    """True when *value* is spelled like a job id. Never raises."""
    return bool(JOB_ID_RE.match(str(value)))
