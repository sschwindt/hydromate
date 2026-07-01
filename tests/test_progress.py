"""Live solver-progress parsing/rendering (:mod:`hydromate.progress`).

Checks that :class:`SolverProgress` reads TELEMAC's listing header
(``ITERATION ... TIME: ... ( <seconds> S)``, written by ``entete.f`` / ``mittit.f``)
into an iteration counter + absolute simulated time, renders a simulated-time
vs DURATION bar, echoes non-header lines verbatim, and falls back to the
day/hour/minute/second decomposition for the run's first minute (where TELEMAC
omits the parenthesised absolute-seconds group).

Pure-python (no subprocess/TELEMAC). Run via:
    mamba run -n hydromate-env pytest tests/test_progress.py
"""

from __future__ import annotations

import io

from hydromate.progress import SolverProgress


def _feed(lines, total=1000.0):
    buf = io.StringIO()   # StringIO.isatty() is False -> non-tty rendering
    p = SolverProgress(total_time=total, out=buf)
    for ln in lines:
        p.feed(ln)
    p.close()
    return p, buf.getvalue()


def test_parses_iteration_and_absolute_time():
    p, out = _feed([
        " ITERATION      500    TIME:      0 H  2 MIN  5.0000 S   (       125.0000 S)",
    ])
    assert p.iteration == 500
    assert p.time == 125.0
    # bar reflects 125/1000 = 12.5 %
    assert "12.5%" in out
    assert "t=125/1,000s" in out and "it=500" in out


def test_advances_and_echoes_non_header_lines():
    p, out = _feed([
        " compile chatter that is not a header",
        " ITERATION     2000    TIME:      0 H  8 MIN 20.0000 S   (       500.0000 S)",
    ])
    assert p.iteration == 2000 and p.time == 500.0
    assert "compile chatter that is not a header" in out   # echoed verbatim
    assert "50.0%" in out


def test_sub_minute_fallback_without_parenthesis():
    # entete.f FORMAT 40 (t < 60 s) prints no "( <sec> S)" group
    p, _ = _feed([" ITERATION      100    TIME:     25.0000 S"])
    assert p.iteration == 100 and p.time == 25.0


def test_time_clamped_to_total():
    p, out = _feed(
        [" ITERATION  9  TIME:  0 H  0 MIN  0.0000 S   (   2000.0000 S)"],
        total=1000.0,
    )
    assert p._frac() == 1.0
    assert "100.0%" in out


def test_unknown_duration_reports_running():
    p, out = _feed([" ITERATION  9  TIME:  0 S   (   500.0000 S)"], total=0.0)
    assert "[running]" in out and "it=9" in out
