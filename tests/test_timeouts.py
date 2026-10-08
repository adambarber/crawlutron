"""Timeouts chokepoint contract: one named place for HTTP timeout budgets.

httpx.Timeout was constructed at four call sites, each inventing its own
connect/read/write/pool numbers (and one site omitted `write`, which httpx
rejects -- a live bug the search-cap RED test surfaced). Every new I/O
site picks a named constructor instead of inventing numbers.
"""

from __future__ import annotations

import dataclasses
from typing import Any

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
        # Bound through Any on purpose: the static checker already forbids this
        # write, and the whole point of the test is that the runtime guard fires
        # where the static one does. So the assignment has to typecheck.
        timeout: Any = Timeouts.for_request(5.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            timeout.read = 99.0
