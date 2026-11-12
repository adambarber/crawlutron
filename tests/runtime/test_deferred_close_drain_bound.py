"""The drain bound: a wedged aclose trips the budget, never hangs shutdown."""

import asyncio
import logging
import time

from crawlutron.runtime.resource_lifecycle import DeferredClose

_LOGGER_NAME = "crawlutron.runtime.resource_lifecycle"
_TIMEOUT_EVENT = "deferred_close_drain_timed_out"


class _Wedged:
    """A resource whose aclose never returns: the shutdown hang shape."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.done = False
        self._gate = asyncio.Event()

    async def aclose(self) -> None:
        self.started.set()
        await self._gate.wait()
        self.done = True


class _Raising:
    """A resource whose aclose raises: the owner path logs it, drain continues."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("boom")


def _tiny_owner(resource: _Wedged) -> DeferredClose[_Wedged]:
    """An owner whose drain budget is a few milliseconds, so tests trip it fast."""
    owner: DeferredClose[_Wedged] = DeferredClose(aclose=_Wedged.aclose, drain_budget_seconds=0.05)
    owner.schedule(resource)
    return owner


async def _await_bounded_drain(caplog, owner):
    """Await one bounded drain under WARNING capture, so tests read its log."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        return await asyncio.wait_for(owner.drain(), timeout=2.0)


def _drain_timeout_records(caplog):
    """Every drain-timeout record a run emitted, so tests assert on the bound."""
    return [r for r in caplog.records if r.getMessage() == _TIMEOUT_EVENT]


async def test_drain_returns_promptly_when_a_close_wedges(caplog):
    """A wedged aclose must trip the bound -- drain returns, it never hangs."""
    wedged = _Wedged()
    owner = _tiny_owner(wedged)
    await wedged.started.wait()

    start = time.monotonic()
    await _await_bounded_drain(caplog, owner)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"drain ignored the budget: {elapsed:.3f}s"
    assert not wedged.done, "the wedged close must be abandoned, not completed"
    timed_out = _drain_timeout_records(caplog)
    assert timed_out, "the drain timeout was not logged"
    assert timed_out[-1].pending == 1, "the log must name the pending count"


async def test_drain_bounds_many_wedged_closes_once(caplog):
    """N wedged closes share one round: the bound fires once, all are logged."""
    wedged = [_Wedged() for _ in range(3)]
    owner: DeferredClose[_Wedged] = DeferredClose(aclose=_Wedged.aclose, drain_budget_seconds=0.05)
    for one in wedged:
        owner.schedule(one)

    before = time.monotonic()
    await _await_bounded_drain(caplog, owner)
    timed_out = _drain_timeout_records(caplog)

    assert time.monotonic() - before < 1.0, "the drain ignored the bound"
    assert len(timed_out) == 1, "the bound must fire once for the whole round"
    assert timed_out[0].pending == 3, "the log must name all pending closes"
    assert all(not one.done for one in wedged), "a wedged close must be abandoned"


async def test_drain_stays_joined_on_a_close_that_raises(caplog):
    """A close that raises (not hangs) is caught by the owner: no timeout."""
    raiser = _Raising()
    owner: DeferredClose[_Raising] = DeferredClose(aclose=_Raising.aclose, drain_budget_seconds=0.05)
    owner.schedule(raiser)

    await _await_bounded_drain(caplog, owner)

    assert raiser.close_calls == 1, "a raising close must still run to completion"
    assert not _drain_timeout_records(caplog), "a raising close must not trip the timeout"
    assert [r for r in caplog.records if r.getMessage() == "deferred_close_failed"], (
        "the raising close's loss must be logged by its owner"
    )


async def test_concurrent_drains_each_respect_the_bound():
    """Two drains on one owner are each bounded: neither hangs shutdown."""
    wedged = _Wedged()
    owner = _tiny_owner(wedged)
    await wedged.started.wait()

    before = time.monotonic()
    results = await asyncio.wait_for(asyncio.gather(owner.drain(), owner.drain()), timeout=2.0)

    assert results == [None, None], "both drains must return"
    assert time.monotonic() - before < 1.0, "a re-entrant drain ignored the bound"
