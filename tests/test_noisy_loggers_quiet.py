"""Third-party parser WARNING spam is suppressed at import time."""

import logging


def test_trafilatura_warnings_suppressed():

    assert not logging.getLogger("trafilatura").isEnabledFor(logging.WARNING)
