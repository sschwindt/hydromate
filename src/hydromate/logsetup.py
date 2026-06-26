"""Compound logging: one timestamped logfile capturing every action.

`setup_logging(logfile)` attaches a file handler (and, by default, a console
handler) to the ``hydromate`` logger and to the ``py.warnings`` logger, so a
single compound logfile records all actions, the per-step timings emitted by
:func:`log_step`, and every warning and error. The file handler logs at DEBUG
(nothing is dropped); the console mirrors it at the chosen level. Python warnings
(``warnings.warn``) are routed in via ``logging.captureWarnings``.

Each script/phase logs into its own output folder: preprocessing.py ->
``preprocessing/hydromate.log``, the build (``pipeline.run``) -> the simulation
(model) folder, and the mesh-convergence study -> ``postprocessing/`` (scoped via
:func:`logging_to`). Append mode, so successive steps accumulate.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("hydromate")

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_TARGET_LOGGERS = ("hydromate", "py.warnings")

# track what we added so the setup is idempotent and resettable (tests)
_added_handlers: list[logging.Handler] = []
_console_added = False
_logfiles: set[str] = set()


def setup_logging(logfile: str | Path | None = None, *, level: int = logging.INFO,
                  console: bool = True, capture_warnings: bool = True) -> Path | None:
    """Route hydromate logs (+ warnings/errors) to a compound logfile and console.

    Idempotent: the console handler is added once per process and each *logfile*
    is attached once, so repeated calls (CLI, pipeline, scripts) don't duplicate
    output. Returns the resolved logfile path (or ``None`` when none was given).
    """
    global _console_added
    formatter = logging.Formatter(_FORMAT, _DATEFMT)
    loggers = [logging.getLogger(name) for name in _TARGET_LOGGERS]
    for lg in loggers:
        lg.setLevel(logging.DEBUG)

    if console and not _console_added:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(formatter)
        for lg in loggers:
            lg.addHandler(ch)
        _added_handlers.append(ch)
        _console_added = True

    path = None
    if logfile is not None:
        path = Path(logfile).resolve()
        if str(path) not in _logfiles:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(path, mode="a", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            for lg in loggers:
                lg.addHandler(fh)
            _added_handlers.append(fh)
            _logfiles.add(str(path))
            log.info("=== hydromate logging started -> %s "
                     "(file: DEBUG, console: %s) ===", path, logging.getLevelName(level))

    if capture_warnings:
        logging.captureWarnings(True)
    return path


def reset_logging() -> None:
    """Remove every handler this module added (used by tests / re-init)."""
    global _console_added
    for lg in (logging.getLogger(name) for name in _TARGET_LOGGERS):
        for h in list(lg.handlers):
            if h in _added_handlers:
                lg.removeHandler(h)
                h.close()
    _added_handlers.clear()
    _logfiles.clear()
    _console_added = False


@contextmanager
def logging_to(logfile: str | Path, *, level: int = logging.INFO, console: bool = True):
    """Scope a compound logfile to a block (for a phase with its own output dir).

    Ensures the console handler, then attaches a file handler for *logfile* for the
    duration of the ``with`` block only, so a later phase logging elsewhere is not
    written here. Used for the mesh-convergence study (logs to postprocessing/).
    """
    setup_logging(level=level, console=console, capture_warnings=True)  # console once
    formatter = logging.Formatter(_FORMAT, _DATEFMT)
    path = Path(logfile).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    loggers = [logging.getLogger(name) for name in _TARGET_LOGGERS]
    for lg in loggers:
        lg.addHandler(handler)
    log.info("=== logging to %s (file: DEBUG) ===", path)
    try:
        yield path
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
        handler.close()


@contextmanager
def log_step(name: str, *, logger: logging.Logger = log, level: int = logging.INFO):
    """Time a calculation step and log its start, duration, or failure.

    Logs ``START <name>``, then ``DONE <name> in N.NNs`` on success or
    ``FAILED <name> after N.NNs: ...`` (and re-raises) on error, so every step's
    elapsed time lands in the compound logfile.
    """
    logger.log(level, "START %s", name)
    t0 = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        logger.error("FAILED %s after %.2fs: %s: %s",
                     name, time.perf_counter() - t0, type(exc).__name__, exc)
        raise
    else:
        logger.log(level, "DONE  %s in %.2fs", name, time.perf_counter() - t0)
