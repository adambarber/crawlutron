"""Timeouts chokepoint contract: one named place for HTTP timeout budgets.
``httpx.Timeout`` was constructed at four sites with invented numbers (one
omitting ``write``, a live bug), so every new I/O site picks a named constructor.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from crawlutron.transport.timeouts import Timeouts


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
        # object.__setattr__ BYPASSES frozen, so the guarded path is the plain
        # write mypy refuses: name it via a def alias (the guard is the subject).
        timeout = Timeouts.for_request(5.0)

        def violating(name: str, value: float) -> None:
            setattr(timeout, name, value)

        with pytest.raises(dataclasses.FrozenInstanceError):
            violating("read", 99.0)


class TestRenderPhaseBudgetPin:
    """The render budget applies per phase, deliberately: navigation has
    returned before the action chain starts, so the bound is sequential."""

    def test_render_phase_seconds_floors_and_preserves(self):
        from crawlutron.transport.timeouts import render_phase_seconds

        assert render_phase_seconds(30) == 30.0
        assert render_phase_seconds(0.2) == 1.0
