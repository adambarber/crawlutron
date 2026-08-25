"""Third-party parser WARNING spam is suppressed at import time."""

import logging


def test_trafilatura_warnings_suppressed():
    import app.main  # noqa: F401  (import applies suppression)

    assert not logging.getLogger("trafilatura").isEnabledFor(logging.WARNING)
