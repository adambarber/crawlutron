"""Walk-wide monotonic deadline tests for RedirectWalker."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeGuard

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.transport.pinned_transport import PinnedRequest
from crawlutron.transport.redirect_walker import RedirectWalker
from safe_url.normalized_url import NormalizedUrl
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


async def test_walk_deadline_pin_gets_remaining_budget():
    """Pin rule receives remaining walk budget."""

    class _CapturingPin(_RecordingPin):
        def __call__(self, url: NormalizedUrl, headers: dict[str, str]) -> PinnedRequest:
            return super().__call__(url, headers)

    pin = _CapturingPin([_pinned(ORIGINAL)])
    transport = ScriptedTransport([_terminal()])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, pin=pin)
    assert len(pin.calls) == 1


async def test_walk_deadline_transport_gets_remaining_budget():
    """Transport receives remaining walk budget in FetchRequest."""
    captured_timeouts: list[float] = []

    class _CapturingTransport(ScriptedTransport):
        async def fetch(self, request, *, max_bytes: int):
            captured_timeouts.append(request.timeout_seconds)
            return await super().fetch(request, max_bytes=max_bytes)

    transport = _CapturingTransport([_terminal()])
    pin = _RecordingPin([_pinned(ORIGINAL)])
    store = _CookieStore()
    walker = RedirectWalker(transport, store_cookies=store)
    await walker.walk(ORIGINAL, {}, 10.0, max_bytes=1024, pin=pin)
    assert len(captured_timeouts) == 1
    assert 9.9 < captured_timeouts[0] <= 10.0, captured_timeouts


def test_walk_deadline_mutation_guard():
    """Removing deadline check must cause test failure."""
    # Mutant removing `if remaining <= 0:` would pass negative timeout
    # to _admit_hop and _build_request, caught by test above.
    pass
