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


class SolverProgress:
    """Echo streamed solver output and render a simulated-time progress bar.

    Feed each stdout line to :meth:`feed`; call :meth:`close` when the run ends.
    On a TTY the bar sticks to the bottom line (rewritten in place with ``\\r``)
    while the raw listing scrolls above it; off a TTY (piped/logged) the raw lines
    are echoed and a bar line is emitted only when the whole-percent advances, so
    the log is not flooded.
    """

    def __init__(self, total_time: float, *, width: int = 40,
                 out: TextIO | None = None):
        self.total_time = float(total_time) if total_time and total_time > 0 else 0.0
        self.width = width
        self.out = out if out is not None else sys.stdout
        self.iteration = 0
        self.time = 0.0
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        self._bar_shown = False
        self._last_len = 0
        self._last_pct = -1

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
        if not self.total_time:
            return 0.0
        return max(0.0, min(self.time / self.total_time, 1.0))

    def _bar_text(self) -> str:
        frac = self._frac()
        filled = int(round(frac * self.width))
        bar = "#" * filled + "-" * (self.width - filled)
        if self.total_time:
            return (f"[{bar}] {frac * 100:5.1f}%  "
                    f"t={self.time:,.0f}/{self.total_time:,.0f}s  it={self.iteration}")
        # no known duration: report raw progress without a fill fraction
        return f"[running] t={self.time:,.0f}s  it={self.iteration}"

    def feed(self, line: str) -> None:
        """Echo *line* and, if it is a listing header, refresh the progress bar."""
        line = line.rstrip("\n")
        is_header = self._parse(line)
        if self.tty:
            if self._bar_shown:
                self.out.write("\r" + " " * self._last_len + "\r")
            self.out.write(line + "\n")
            bar = self._bar_text()
            self.out.write(bar)
            self._last_len = len(bar)
            self._bar_shown = True
            self.out.flush()
        else:
            self.out.write(line + "\n")
            if is_header:
                pct = int(self._frac() * 100)
                if pct != self._last_pct:
                    self._last_pct = pct
                    self.out.write(self._bar_text() + "\n")
            self.out.flush()

    def close(self) -> None:
        """Finish the bar so later output does not overwrite it."""
        if self.tty and self._bar_shown:
            self.out.write("\n")
            self.out.flush()
            self._bar_shown = False
