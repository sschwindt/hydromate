"""Compound logfile: per-step timing, and captured warnings/errors.

Checks that :func:`axqua.setup_logging` writes a single logfile and that
:func:`axqua.log_step` records each step's start, elapsed time, or failure,
and that warnings and errors land in the same file.

Pure-python (stdlib logging). Run via:
    mamba run -n axqua-env pytest tests/test_logging.py
"""

from __future__ import annotations

import logging

from axqua import log_step, logging_to, setup_logging

log = logging.getLogger("axqua")


def _flush():
    for h in logging.getLogger("axqua").handlers:
        h.flush()


def test_compound_logfile_captures_steps_and_levels(tmp_path):
    logfile = setup_logging(tmp_path / "axqua.log", console=False)
    assert logfile is not None and logfile.exists()

    with log_step("demo calculation"):
        log.info("doing work")
    log.warning("STABILITY RISK example")
    log.error("an error happened")
    _flush()

    text = logfile.read_text()
    assert "START demo calculation" in text
    assert "DONE  demo calculation in" in text          # timing recorded
    assert "s\n" in text or "s " in text                # elapsed seconds printed
    assert "INFO" in text and "doing work" in text
    assert "WARNING" in text and "STABILITY RISK example" in text
    assert "ERROR" in text and "an error happened" in text
    # timestamps are present (YYYY-MM-DD HH:MM:SS prefix)
    assert text[:4].isdigit() and text[4] == "-"


def test_log_step_records_failure(tmp_path):
    logfile = setup_logging(tmp_path / "run.log", console=False)
    try:
        with log_step("risky step"):
            raise ValueError("kaboom")
    except ValueError:
        pass
    _flush()

    text = logfile.read_text()
    assert "FAILED risky step after" in text
    assert "ValueError" in text and "kaboom" in text


def test_setup_logging_is_idempotent(tmp_path):
    p1 = setup_logging(tmp_path / "one.log", console=False)
    # same file again should not add a second handler
    p2 = setup_logging(tmp_path / "one.log", console=False)
    assert p1 == p2
    file_handlers = [h for h in logging.getLogger("axqua").handlers
                     if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_logging_to_is_scoped(tmp_path):
    """A logging_to block (a phase's own logfile) captures only its own messages;
    a separately set-up logfile keeps receiving everything (per-phase separation)."""
    outer = setup_logging(tmp_path / "build.log", console=False)   # e.g. simulation/
    log.info("before block")
    with logging_to(tmp_path / "postproc.log"):                    # e.g. postprocessing/
        log.info("inside the convergence study")
    log.info("after block")
    _flush()

    scoped = (tmp_path / "postproc.log").read_text()
    build = outer.read_text()
    assert "inside the convergence study" in scoped
    assert "before block" not in scoped and "after block" not in scoped
    # the persistent build log still sees the after-block message
    assert "after block" in build


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    test_compound_logfile_captures_steps_and_levels(Path(tempfile.mkdtemp()))
    print("LOGGING TESTS PASSED")
