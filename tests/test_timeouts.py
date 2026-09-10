"""Timeouts chokepoint contract: one named place for HTTP timeout budgets.

httpx.Timeout was constructed at four call sites, each inventing its own
connect/read/write/pool numbers (and one site omitted `write`, which httpx
rejects -- a live bug the search-cap RED test surfaced). Every new I/O
site picks a named constructor instead of inventing numbers.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.timeouts import Timeouts


class TestTimeoutsChokepoint:
    def test_for_request_honors_read_budget(self):
        timeouts = Timeouts.for_request(12.0)
        assert timeouts.read == 12.0
        assert timeouts.connect == 5.0
        assert timeouts.write == 10.0
        assert timeouts.pool == 5.0

    def test_as_httpx_carries_all_four_parameters(self):
        import httpx

        timeout = Timeouts.for_request(12.0).as_httpx()
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 12.0
        assert timeout.connect == 5.0
        assert timeout.write == 10.0
        assert timeout.pool == 5.0

    def test_defaults_match_service_posture(self):
        assert Timeouts.defaults().read == 30.0

    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            Timeouts.for_request(5.0).read = 99.0  # type: ignore[misc]
