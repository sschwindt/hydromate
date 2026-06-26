"""Shared pytest fixtures."""

import pytest

from hydromate.logsetup import reset_logging


@pytest.fixture(autouse=True)
def _reset_hydromate_logging():
    """Drop any logging handlers a test (or pipeline.run) attached, so per-test
    logfiles in tmp dirs don't leak handlers into later tests."""
    yield
    reset_logging()
