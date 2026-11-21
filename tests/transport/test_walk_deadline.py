"""Walk-wide monotonic deadline tests for RedirectWalker."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeGuard

import pytest

import crawlutron.transport.redirect_walker as _walker_mod
from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.transport import pinned_dns
from crawlutron.transport.pinned_transport import PinnedRequest
from crawlutron.transport.redirect_walker import RedirectWalker
from safe_url.normalized_url import NormalizedUrl
from tests._clocks import Timeline
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import ScriptedTransport
from tests._policy_factories import canonical
from tests.transport.test_redirect_walker import _CookieStore, _pinned, _RecordingPin

ORIGINAL = canonical("http://a.com/x")
TARGET = canonical("http://a.com/y")
THIRD = canonical("http://a.com/z")


def _result(
    status: int = 200,
    *,
    url: NormalizedUrl = ORIGINAL,
    final_url: NormalizedUrl | None = None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    location: str | None = None,
) -> HttpFetchResult:
    merged = dict(headers or {})
    if location is not None:
        merged["location"] = location
    return make_fetch(
        status=status,
        body=body,
        url=url,
        final_url=final_url if final_url is not None else url,
        headers=merged,
    )


def _redirect(location: str = "/y", url: NormalizedUrl = ORIGINAL) -> HttpFetchResult:
    return _result(302, url=url, final_url=url, headers={"set-cookie": "hop=1"}, location=location)


def _terminal(
    body: bytes = b"ok",
    url: NormalizedUrl = TARGET,
    headers: dict[str, str] | None = None,
) -> HttpFetchResult:
    return _result(200, url=url, final_url=url, body=body, headers=headers)


class _AdvancingTransport(ScriptedTransport):
    """Scripted transport that advances the walker's timeline after each hop."""

    def __init__(self, script, timeline: Timeline, advance: float) -> None:
        super().__init__(script)
        self._timeline = timeline
        self._advance = advance

    async def fetch(self, request, *, max_bytes: int) -> HttpFetchResult:
        result = await super().fetch(request, max_bytes=max_bytes)
        self._timeline.advance(self._advance)
        return result


class _CapturingTransport(ScriptedTransport):
    """Captures each hop's timeout_seconds into captured_timeouts."""

    def __init__(self, script, timeline: Timeline, advance: float = 0.0) -> None:
        super().__init__(script)
        self._timeline = timeline
        self._advance = advance
        self.captured_timeouts: list[float] = []

    async def fetch(self, request, *, max_bytes: int):
        self.captured_timeouts.append(request.timeout_seconds)
        result = await super().fetch(request, max_bytes=max_bytes)
        self._timeline.advance(self._advance)
        return result


async def _run_walk(
    script: Sequence[HttpFetchResult | BaseException],
    pin_responses: Sequence[PinnedRequest],
    *,
    max_bytes: int = 1024,
    url: NormalizedUrl = ORIGINAL,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
    store: _CookieStore | None = None,
) -> tuple[HttpFetchResult | None, ScriptedTransport, _RecordingPin, _CookieStore]:
    transport = ScriptedTransport(list(script))
    pin = _RecordingPin(list(pin_responses))
    store = _CookieStore() if store is None else store
    walker = RedirectWalker(transport, store_cookies=store)
    result = await walker.walk(
        url,
        headers if headers is not None else {},
        timeout_seconds,
        max_bytes=max_bytes,
        pin=pin,
    )
    return result, transport, pin, store


def _assert_http_408(result: HttpFetchResult | None) -> TypeGuard[HttpFetchResult]:
    assert result is not None
    assert result.status_code == 408, result
    assert result.body == b""
    return True


async def test_walk_deadline_exhausted_before_first_hop():
    """Deadline exhausted before first hop returns 408."""
    result, transport, pin, store = await _run_walk([_terminal()], [_pinned(ORIGINAL)], timeout_seconds=0.0)
    assert _assert_http_408(result)
    assert result.final_url == ORIGINAL


async def test_walk_deadline_allows_first_hop_when_time_remains():
    """Sufficient time allows first hop to proceed."""
    result, transport, pin, store = await _run_walk(
        [_terminal(url=ORIGINAL)], [_pinned(ORIGINAL)], timeout_seconds=10.0
    )
    assert result is not None and result.status_code == 200
    assert result.url == ORIGINAL


async def test_walk_deadline_allows_multi_hop_when_time_remains():
    """Sufficient time allows multiple hops to proceed."""
    result, transport, pin, store = await _run_walk(
        [_redirect("/y"), _terminal(url=TARGET)],
        [_pinned(ORIGINAL), _pinned(TARGET)],
        timeout_seconds=10.0,
    )
    assert result is not None and result.status_code == 200
    assert result.url == TARGET


async def test_walk_deadline_boundary_0_1_hop():
    """Boundary: 0 hops vs 1 hop both succeed when time remains."""
    result, transport, pin, store = await _run_walk(
        [_terminal(url=ORIGINAL)], [_pinned(ORIGINAL)], timeout_seconds=10.0
    )
    assert result is not None and result.status_code == 200
    assert result.url == ORIGINAL

    result, transport, pin, store = await _run_walk(
        [_redirect("/y"), _terminal(url=TARGET)], [_pinned(ORIGINAL), _pinned(TARGET)], timeout_seconds=10.0
    )
    assert result is not None and result.status_code == 200
    assert result.url == TARGET


async def test_walk_deadline_exhausted_returns_408_not_hang():
    """Exhausted deadline returns 408, does not hang."""
    result, transport, pin, store = await _run_walk([_terminal()], [_pinned(ORIGINAL)], timeout_seconds=0.0)
    assert _assert_http_408(result)
    assert len(transport.calls) == 0


@pytest.mark.parametrize("bad_timeout", [-1.0, float("nan"), float("inf")])
async def test_walk_deadline_refuses_bad_timeout(bad_timeout):
    """Non-finite or negative timeout_seconds is refused at the walk door."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        await _run_walk([_terminal()], [_pinned(ORIGINAL)], timeout_seconds=bad_timeout)


async def test_walk_deadline_walks_with_positive_timeout():
    """Positive finite timeout_seconds walks normally."""
    result, transport, pin, store = await _run_walk(
        [_terminal(url=ORIGINAL)], [_pinned(ORIGINAL)], timeout_seconds=5.0
    )
    assert result is not None and result.status_code == 200
    assert result.url == ORIGINAL


async def test_walk_deadline_hop2_gets_reduced_remainder(monkeypatch):
    """P0-1(a): hop 2's timeout is the reduced remainder (deadline pinned once).

    A per-hop deadline reset mutant is killed by simulating elapsed time.
    """
    transport = _CapturingTransport([_redirect("/y"), _terminal(url=TARGET)], Timeline(0.0), advance=3.0)
    monkeypatch.setattr(_walker_mod.time, "monotonic", transport._timeline)
    pin = _RecordingPin([_pinned(ORIGINAL), _pinned(TARGET)])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    result = await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, pin=pin)
    assert result is not None and result.status_code == 200, result
    assert len(transport.captured_timeouts) == 2, transport.captured_timeouts
    assert transport.captured_timeouts[0] == 10.0, transport.captured_timeouts
    assert transport.captured_timeouts[1] == 7.0, transport.captured_timeouts
    assert transport.captured_timeouts[1] < 10.0, transport.captured_timeouts


async def test_walk_deadline_chain_overrun_returns_408(monkeypatch):
    """P0-1(b): redirect chain overrun returns 408, final_url = last fetched URL.

    final_url names ORIGINAL (fetched), never TARGET (undialed).
    """
    timeline = Timeline(0.0)
    monkeypatch.setattr(_walker_mod.time, "monotonic", timeline)
    transport = _AdvancingTransport([_redirect("/y"), _terminal(url=TARGET)], timeline, advance=15.0)
    pin = _RecordingPin([_pinned(ORIGINAL), _pinned(TARGET)])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    result = await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, pin=pin)
    assert _assert_http_408(result)
    assert result.final_url == ORIGINAL, result.final_url
    assert len(transport.calls) == 1, transport.calls


async def test_walk_deadline_cookie_not_stored_on_408(monkeypatch):
    """P0-1(c): Set-Cookie not stored when 408 pre-empts next admit.

    Hop 2's admit never runs, so the deferred cookie is never flushed.
    """
    timeline = Timeline(0.0)
    monkeypatch.setattr(_walker_mod.time, "monotonic", timeline)
    transport = _AdvancingTransport([_redirect("/y"), _terminal(url=TARGET)], timeline, advance=15.0)
    pin = _RecordingPin([_pinned(ORIGINAL), _pinned(TARGET)])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    result = await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, pin=pin)
    assert _assert_http_408(result)
    assert len(store.calls) == 0, store.calls
    assert len(transport.calls) == 1, transport.calls


async def test_walk_deadline_transport_gets_remaining_budget(monkeypatch):
    """P1-2: first hop gets remaining wall budget (float <= stated).

    Frozen clock leaving ~3s; assertion is strict (< 10.0).
    """
    transport = _CapturingTransport([_terminal()], Timeline(7.0), advance=0.0)
    monkeypatch.setattr(_walker_mod.time, "monotonic", transport._timeline)
    pin = _RecordingPin([_pinned(ORIGINAL)])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, started=0.0, pin=pin)
    assert len(transport.captured_timeouts) == 1, transport.captured_timeouts
    assert transport.captured_timeouts[0] == 3.0, transport.captured_timeouts
    assert transport.captured_timeouts[0] < 10.0, transport.captured_timeouts


async def test_walk_deadline_pin_gets_remaining_budget(monkeypatch):
    """P1-3: pin resolve bounded by remaining walk budget, not full stated.

    Asserts the timeout_seconds _admit_hop passes to bounded_thread_resolve.
    """
    captured_pin_timeouts: list[float] = []
    original_resolve = pinned_dns.bounded_thread_resolve

    async def capturing_resolve(work, *, timeout_seconds: float):
        captured_pin_timeouts.append(timeout_seconds)
        return await original_resolve(work, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(pinned_dns, "bounded_thread_resolve", capturing_resolve)

    timeline = Timeline(7.0)
    monkeypatch.setattr(_walker_mod.time, "monotonic", timeline)
    transport = ScriptedTransport([_terminal()])
    pin = _RecordingPin([_pinned(ORIGINAL)])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, started=0.0, pin=pin)
    assert len(captured_pin_timeouts) == 1, captured_pin_timeouts
    assert captured_pin_timeouts[0] == 3.0, captured_pin_timeouts
    assert captured_pin_timeouts[0] < 10.0, captured_pin_timeouts
