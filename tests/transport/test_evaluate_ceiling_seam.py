"""The evaluate ceiling reads through an injectable seam so tests can
bound a wedged render in 0.1s instead of waiting out the real 5s ceiling;
production keeps the 5.0 default."""

from __future__ import annotations

import asyncio
import time

import pytest

from crawlutron.transport import timeouts
from tests._browser_doubles import BrowserFakePage

_HANG_GUARD_SECONDS = 8.0


def _never_returns():
    """The awaitable shape every ceiling test cancels."""

    async def op():
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the ceiling must cancel this read")

    return op()


class _NeverReturningPage(BrowserFakePage):
    """A page whose evaluate never returns: the shape the ceiling must cancel."""

    async def evaluate(self, expression, *a, **k):
        await _never_returns()


async def test_ceiling_injection_cancels_a_wedged_evaluate(monkeypatch):
    """A 0.1s injected ceiling cancels a never-returning evaluate promptly --
    the seam the wedge suites ride instead of the real 5s ceiling."""
    monkeypatch.setattr(timeouts, "EVALUATE_CEILING_SECONDS_OVERRIDE", 0.1)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            timeouts.bounded_evaluate(_NeverReturningPage(), "() => document.title"),
            timeout=_HANG_GUARD_SECONDS,
        )
    assert time.monotonic() - started < 2.0, "the injected ceiling did not trip"


async def test_ceiling_injection_cancels_a_wedged_content_read(monkeypatch):
    """bounded_content rides the same seam: a 0.1s ceiling cancels a
    never-returning page.content()."""

    class _WedgedContentPage(BrowserFakePage):
        async def content(self):
            await asyncio.Event().wait()
            raise AssertionError("unreachable: the ceiling must cancel this read")

    monkeypatch.setattr(timeouts, "EVALUATE_CEILING_SECONDS_OVERRIDE", 0.1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            timeouts.bounded_content(_WedgedContentPage()),
            timeout=_HANG_GUARD_SECONDS,
        )


def test_production_ceiling_is_unchanged_and_seam_defaults_off():
    """The production constant stays 5.0 and the seam defaults to unset, so
    the override is a test-only knob, never a silently lowered posture."""
    assert timeouts.MAX_EVALUATE_TIMEOUT_SECONDS == 5.0
    assert timeouts.EVALUATE_CEILING_SECONDS_OVERRIDE is None


async def test_without_injection_the_constant_bounds_the_read(monkeypatch):
    """With the seam unset, bounded_read reads MAX_EVALUATE_TIMEOUT_SECONDS
    itself (pinned to 0.1 here to make the read observable without waiting
    out the real 5s ceiling; the production value is pinned separately)."""
    monkeypatch.setattr(timeouts, "MAX_EVALUATE_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await timeouts.bounded_read(_never_returns())
    assert time.monotonic() - started < 2.0, "bounded_read ignored the ceiling constant"
