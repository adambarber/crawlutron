"""Shared test helpers for forcing non-UTC timezone in tests."""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager


@contextmanager
def force_tz_america_new_york() -> Generator[None]:
    """Force TZ=America/New_York for the duration of the context.

    Calls time.tzset() to apply the change and restores the prior TZ afterward.
    """
    prior_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    try:
        time.tzset()
    except AttributeError:
        # Windows doesn't have tzset; the test will still run but TZ won't take effect.
        # This is acceptable because the vulnerability is Linux/macOS specific.
        pass
    try:
        yield
    finally:
        if prior_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior_tz
        try:
            time.tzset()
        except AttributeError:
            pass


def pytest_configure(config):
    """Register the non_utc_tz fixture."""
    # This is called by pytest; the fixture is defined below for direct use.


import pytest


@pytest.fixture
def non_utc_tz() -> Generator[None]:
    """Fixture that forces TZ=America/New_York for the test duration.

    Use this fixture on tests that need to verify UTC-normalization behavior
    under a non-UTC local timezone. The fixture restores the original TZ after.
    """
    with force_tz_america_new_york():
        yield
