"""Timeouts chokepoint contract: one named place for HTTP timeout budgets.
``httpx.Timeout`` was constructed at four sites with invented numbers (one
omitting ``write``, a live bug), so every new I/O site picks a named constructor.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from app.transport.timeouts import Timeouts


class TestTimeoutsChokepoint:
    def test_for_request_honors_read_budget(self):
        timeouts = Timeouts.for_request(12.0)
        assert timeouts.read == 12.0
        assert timeouts.connect == 5.0
        assert timeouts.write == 10.0
        assert timeouts.pool == 5.0

    def test_as_httpx_carries_all_four_parameters(self):
        timeout = Timeouts.for_request(12.0).as_httpx()
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 12.0
        assert timeout.connect == 5.0
        assert timeout.write == 10.0
        assert timeout.pool == 5.0

    def test_defaults_match_service_posture(self):
        assert Timeouts.defaults().read == 30.0

    def test_frozen(self):
        # Bound through object: static checker forbids the write; the test asserts
        # the runtime guard fires where the static one does.
        timeout: object = Timeouts.for_request(5.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            timeout.read = 99.0
