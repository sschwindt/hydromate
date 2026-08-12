"""Live progress reporting for a streaming TELEMAC solver run.

TELEMAC's launcher (``telemac2d.py`` / ``telemac3d.py``) prints its listing to
stdout as the run marches. hydromate normally captures that silently, so a solver
run looks like total silence in the terminal. :class:`SolverProgress` instead
echoes every line as it arrives (so the run is not a black box) and, by parsing the
per-listing header, overlays a single-line progress bar of **simulated time against
the run's DURATION** - the point the variable-time-step march reaches when no
steady-state stop criterion fires.

The header is written by TELEMAC's ``entete.f`` (2D) / ``mittit.f`` (3D) as, e.g.::

     ITERATION      500    TIME:      0 H  2 MIN  5.0000 S   (       125.0000 S)

so the iteration counter is the integer after ``ITERATION`` and the absolute
simulated time is the value in the trailing ``( ... S)`` group (with a
day/hour/minute/second fallback for the first minute, where TELEMAC omits that
group).
"""

from __future__ import annotations

import re
import sys
from typing import TextIO

# integer iteration counter, e.g. "ITERATION      500"
_ITER_RE = re.compile(r"ITERATION\s+(\d+)")
# absolute simulated seconds in the trailing parenthesis, e.g. "(   125.0000 S)"
_ABS_RE = re.compile(r"\(\s*([0-9.]+)\s*S\s*\)")
# fallback for the run's first minute (entete.f FORMAT 40 has no parenthesis group):
# "TIME:  [<n> D ][<n> H ][<n> M[IN|N] ]<sec> S"
_DHMS_RE = re.compile(
    r"TIME:\s*"
    r"(?:(\d+)\s*D\s+)?"
    r"(?:(\d+)\s*H\s+)?"
    r"(?:(\d+)\s*M(?:IN|N)\s+)?"
    r"([0-9.]+)\s*S"
)


class ProgressBar:
    """Terminal rendering for a streamed solver run - the part no solver owns.

    On a TTY the bar sticks to the bottom line (rewritten in place with ``\\r``)
    while the raw listing scrolls above it; off a TTY (piped/logged) the raw lines
    are echoed and a bar line is emitted only when the whole-percent advances, so
    the log is not flooded.

    Split out of :class:`SolverProgress` so the OpenFOAM renderer
    (:class:`hydromate.openfoam.runtime.OpenFoamProgress`), which parses a completely
    different listing, presents an identical bar rather than a second look-alike
    implementation. Callers :meth:`echo` each raw line and :meth:`update` the
    position; :class:`SolverProgress` keeps doing both from one :meth:`feed`.
    """

    def __init__(self, total: float, *, width: int = 40, out: TextIO | None = None):
        self.total = float(total) if total and total > 0 else 0.0
        self.width = width
        self.out = out if out is not None else sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        self.position = 0.0
        self.suffix = ""
        self._bar_shown = False
        self._last_len = 0
        self._last_pct = -1
        self._pending = False

    def _frac(self) -> float:
        if not self.total:
            return 0.0
        return max(0.0, min(self.position / self.total, 1.0))

    def text(self) -> str:
        if not self.total:
            return f"[running] {self.suffix}"
        frac = self._frac()
        filled = int(round(frac * self.width))
        bar = "#" * filled + "-" * (self.width - filled)
        return f"[{bar}] {frac * 100:5.1f}%  {self.suffix}"

    def echo(self, line: str) -> None:
        """Print one raw output line, keeping the bar at the bottom on a TTY."""
        line = line.rstrip("\n")
        if self.tty:
            if self._bar_shown:
                self.out.write("\r" + " " * self._last_len + "\r")
                self._bar_shown = False
            self.out.write(line + "\n")
            self._pending = True
        else:
            self.out.write(line + "\n")
        self.out.flush()

    def update(self, position: float, suffix: str = "", *,
               force: bool = False) -> None:
        """Move the bar to *position* (in the same units as ``total``)."""
        self.position = float(position)
        if suffix:
            self.suffix = suffix
        if self.tty:
            bar = self.text()
            self.out.write(bar)
            self._last_len = len(bar)
            self._bar_shown = True
            self._pending = False
            self.out.flush()
            return
        pct = int(self._frac() * 100)
        if force or pct != self._last_pct:
            self._last_pct = pct
            self.out.write(self.text() + "\n")
            self.out.flush()

    def close(self) -> None:
        """Finish the bar so later output does not overwrite it."""
        if self.tty and (self._bar_shown or self._pending):
            self.out.write("\n")
            self.out.flush()
            self._bar_shown = False
            self._pending = False


class SolverProgress:
    """Echo streamed TELEMAC output and render a simulated-time progress bar.

    Feed each stdout line to :meth:`feed`; call :meth:`close` when the run ends.
    The terminal rendering is :class:`ProgressBar`; this class owns only the
    TELEMAC listing parsing.
    """

    def __init__(self, total_time: float, *, width: int = 40,
                 out: TextIO | None = None):
        self.total_time = float(total_time) if total_time and total_time > 0 else 0.0
        self.bar = ProgressBar(self.total_time, width=width, out=out)
        self.iteration = 0
        self.time = 0.0

    # the terminal state lives on the shared bar; these keep the old attribute names
    @property
    def width(self) -> int:
        return self.bar.width

    @property
    def out(self) -> TextIO:
        return self.bar.out

    @property
    def tty(self) -> bool:
        return self.bar.tty

    # -- parsing -----------------------------------------------------------
    def _parse(self, line: str) -> bool:
        """Update iteration/time from a listing header; return True if it was one."""
        m = _ITER_RE.search(line)
        if not m:
            return False
        self.iteration = int(m.group(1))
        t = self._parse_time(line)
        if t is not None:
            self.time = t
        return True

    @staticmethod
    def _parse_time(line: str) -> float | None:
        m = _ABS_RE.search(line)
        if m:
            return float(m.group(1))
        m = _DHMS_RE.search(line)
        if m:
            days, hours, mins, secs = m.groups()
            return (int(days or 0) * 86400 + int(hours or 0) * 3600
                    + int(mins or 0) * 60 + float(secs))
        return None

    # -- rendering ---------------------------------------------------------
    def _frac(self) -> float:
        self.bar.position = self.time
        return self.bar._frac()

    def _suffix(self) -> str:
        if self.total_time:
            return (f"t={self.time:,.0f}/{self.total_time:,.0f}s  "
                    f"it={self.iteration}")
        # no known duration: report raw progress without a fill fraction
        return f"t={self.time:,.0f}s  it={self.iteration}"

    def _bar_text(self) -> str:
        self.bar.position = self.time
        self.bar.suffix = self._suffix()
        return self.bar.text()

    def feed(self, line: str) -> None:
        """Echo *line* and, if it is a listing header, refresh the progress bar."""
        line = line.rstrip("\n")
        is_header = self._parse(line)
        self.bar.echo(line)
        # off a TTY only a header can have moved the clock, so the log is not
        # flooded with an unchanged bar after every line of solver chatter
        if self.bar.tty or is_header:
            self.bar.update(self.time, self._suffix())

    def close(self) -> None:
        """Finish the bar so later output does not overwrite it."""
        self.bar.close()
