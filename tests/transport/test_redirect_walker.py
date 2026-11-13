"""Direct unit pins for ``RedirectWalker``: cookie capture lives in the walker, one stream cap per hop,
``BodyTooLarge`` -> http_413 with a logical ``final_url``, re-pin per hop, and the declared-length gate
passing through.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeGuard

import httpx
import pytest
from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.safety.bounded_io import BodyTooLarge
from crawlutron.transport.pinned_transport import PinnedRequest
from crawlutron.transport.redirect_walker import RedirectWalker
from crawlutron.transport.transport import http_413
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import ScriptedTransport
from tests._policy_factories import canonical

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
    """One hop result; headers are what the walker captures cookies from."""
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


def _terminal(
    body: bytes = b"ok", url: NormalizedUrl = TARGET, headers: dict[str, str] | None = None
) -> HttpFetchResult:
    return _result(200, url=url, final_url=url, body=body, headers=headers)


def _redirect(location: str = "/y", url: NormalizedUrl = ORIGINAL) -> HttpFetchResult:
    return _result(302, url=url, final_url=url, headers={"set-cookie": "hop=1"}, location=location)


class _CookieStore:
    """The injected ``store_cookies`` seam; records each (url, headers) planting."""

    def __init__(self) -> None:
        self.calls: list[tuple[NormalizedUrl, dict[str, str]]] = []

    async def __call__(self, url: NormalizedUrl, headers: Mapping[str, str]) -> None:
        self.calls.append((url, dict(headers)))


class _RecordingPin:
    """Injected ``pin`` rule recording each (url, headers) admission and
    answering with a pre-built ``PinnedRequest``."""

    def __init__(self, responses: list[PinnedRequest]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[NormalizedUrl, dict[str, str]]] = []

    def __call__(self, url: NormalizedUrl, headers: dict[str, str]) -> PinnedRequest:
        self.calls.append((url, dict(headers)))
        return self._responses.pop(0)


def _pinned(url: NormalizedUrl = ORIGINAL, connect_url: str | None = None) -> PinnedRequest:
    return PinnedRequest(
        connect_url=connect_url if connect_url is not None else url.raw,
        headers={"Host": url.host},
        url=url,
    )


async def _walk(
    transport: ScriptedTransport,
    store: _CookieStore,
    pin: _RecordingPin | None = None,
    max_bytes: int = 1024,
    url: NormalizedUrl = ORIGINAL,
    headers: dict[str, str] | None = None,
) -> HttpFetchResult | None:
    walker = RedirectWalker(transport, store_cookies=store)
    return await walker.walk(url, headers if headers is not None else {}, 10, max_bytes=max_bytes, pin=pin)


def _assert_http_413(result: HttpFetchResult | None) -> TypeGuard[HttpFetchResult]:
    """A refused (capped) hop: present, terminal 413, empty body (narrows result)."""
    assert result is not None
    assert result.status_code == 413, result
    assert result.body == b""
    return True


async def _run_walk(
    script: Sequence[HttpFetchResult | BaseException],
    pin_responses: Sequence[PinnedRequest],
    *,
    max_bytes: int = 1024,
    url: NormalizedUrl = ORIGINAL,
    headers: dict[str, str] | None = None,
    store: _CookieStore | None = None,
) -> tuple[HttpFetchResult | None, ScriptedTransport, _RecordingPin, _CookieStore]:
    """Build the walker's doubles, run one walk, return result plus doubles."""
    transport = ScriptedTransport(list(script))
    pin = _RecordingPin(list(pin_responses))
    store = _CookieStore() if store is None else store
    result = await _walk(transport, store, pin=pin, max_bytes=max_bytes, url=url, headers=headers)
    return result, transport, pin, store


async def test_cookie_capture_lives_in_walker_not_backend():
    """C1 structural pin: the walker plants each hop's Set-Cookie exactly once per hop, and the transport
    never touches cookies. Hop 1 is a 302 carrying Set-Cookie hop=1, hop 2 terminal carrying hop=2; hop
    1's cookie defers until hop 2's pin admits.
    """
    result, transport, pin, store = await _run_walk(
        [_redirect("/y"), _terminal(headers={"set-cookie": "hop=2"})],
        [_pinned(ORIGINAL), _pinned(TARGET)],
    )

    assert result is not None and result.status_code == 200
    # Hop 1's cookie is deferred until hop 2's pin admits; planted on hop 1.
    # Hop 2's terminal cookie is planted on hop 2's logical URL.
    assert len(store.calls) == 2, store.calls
    # The transport never captures cookies -- cookie capture is the walker's seam.
    assert store.calls[0][0] == ORIGINAL, store.calls[0]
    assert store.calls[0][1]["set-cookie"] == "hop=1", store.calls[0][1]
    assert store.calls[1][0] == TARGET, store.calls[1]
    assert store.calls[1][1]["set-cookie"] == "hop=2", store.calls[1][1]
    assert len(transport.calls) == 2, transport.calls


async def test_stream_cap_applied_once_per_hop_with_fresh_budget():
    """The stream cap is applied per hop at ``max_bytes`` each: a redirect hop reads no body, so the
    terminal hop receives a full fresh budget. When the terminal hop raises ``BodyTooLarge`` the
    walker maps it to http_413 with a logical ``final_url`` -- never the pinned IP/connect URL."""
    max_bytes = 100
    result, transport, pin, store = await _run_walk(
        [_redirect("/y"), BodyTooLarge("streamed body exceeds cap")],
        [
            _pinned(ORIGINAL, connect_url="http://1.2.3.4/x"),
            _pinned(TARGET, connect_url="http://1.2.3.4/y"),
        ],
        max_bytes=max_bytes,
    )
    assert _assert_http_413(result)
    # final_url is the logical URL of the hop that breached, never the pinned IP.
    assert result.final_url == TARGET, result.final_url
    assert str(result.final_url) != "http://1.2.3.4/y"
    # Each hop received the same full budget -- the cap is per hop, not cumulative.
    assert all(call[1] == max_bytes for call in transport.calls)


async def test_body_exactly_at_cap_passes():
    """A terminal body of exactly ``max_bytes`` is not breached (the cap is a
    strict ``>``), so a full-sized body on the hop that reads it is admitted."""
    max_bytes = 50
    result, transport, pin, store = await _run_walk(
        [_terminal(body=b"A" * max_bytes, url=TARGET)], [_pinned(TARGET)], max_bytes=max_bytes
    )

    assert result is not None and result.status_code == 200
    assert result.body == b"A" * max_bytes
    assert [call[1] for call in transport.calls] == [max_bytes]


async def test_body_over_cap_breaches_on_first_hop():
    """A terminal body over the cap on the first hop yields http_413 with a
    logical ``final_url`` equal to that hop's logical URL."""
    result, transport, pin, store = await _run_walk(
        [BodyTooLarge("streamed body exceeds cap")], [_pinned(TARGET)], max_bytes=10
    )
    assert _assert_http_413(result)
    # First (and only) hop breached: logical final_url is that hop's own URL.
    assert result.final_url == ORIGINAL


async def test_re_pin_per_redirect_hop():
    """The pin rule is re-run for every redirect hop with the current (advanced) logical URL --
    re-validation per hop, not a single admission pinned to the original URL. Headers ride each
    re-pin within one origin."""
    result, transport, pin, store = await _run_walk(
        [_redirect("/y"), _terminal()],
        [_pinned(ORIGINAL), _pinned(TARGET)],
        headers={"Host": "a.com"},
    )

    assert result is not None and result.status_code == 200
    assert len(pin.calls) == 2, pin.calls
    assert pin.calls[0][0] == ORIGINAL, pin.calls
    assert pin.calls[1][0] == TARGET, pin.calls
    # Headers ride each re-pin unchanged within one origin.
    assert pin.calls[0][1] == {"Host": "a.com"}, pin.calls
    assert pin.calls[1][1] == {"Host": "a.com"}, pin.calls


async def test_declared_length_gate_413_passes_through_as_terminal():
    """When a transport's declared-length gate emits an http_413 result
    (instead of raising), the walker returns it as the terminal result
    unchanged -- the walker does not re-cap or mangle it."""
    refusal = http_413(ORIGINAL, TARGET, 0.0)
    result, transport, pin, store = await _run_walk([refusal], [_pinned(TARGET)])
    assert _assert_http_413(result)
    assert result.final_url == TARGET
    assert result is refusal


async def test_terminal_url_is_terminal_hop_logical_not_original_admitted():
    """Post-#685 contract: a terminal result's ``url`` is the terminal-hop
    logical URL, not the original admitted URL; ``final_url`` is unchanged and
    equal to it. Walking a 302 to /y then a 200 lands on /y for both fields."""
    script = [_redirect("/y"), _terminal()]
    pins = [_pinned(ORIGINAL), _pinned(TARGET)]
    result, transport, pin, store = await _run_walk(script, pins)

    assert result is not None
    assert result.status_code == 200
    assert result.url == TARGET, result.url
    assert result.final_url == TARGET, result.final_url
    assert result.url != ORIGINAL
    assert result.final_url != ORIGINAL


async def test_empty_chain_returns_the_first_response_terminal():
    """Empty chain: a terminal first hop ends the walk with one fetch, no advance."""
    result, transport, *_ = await _run_walk(
        [_terminal(url=ORIGINAL)], [_pinned(ORIGINAL)], store=_CookieStore()
    )
    assert result is not None and result.status_code == 200
    assert result.url == ORIGINAL
    assert result.final_url == ORIGINAL
    assert len(transport.calls) == 1


async def test_one_hop_reaches_its_terminal():
    """One hop: a single redirect then a terminal answer; final URL is /y."""
    result, transport, pin, *_ = await _run_walk(
        [_redirect("/y"), _terminal()], [_pinned(ORIGINAL), _pinned(TARGET)], store=_CookieStore()
    )
    assert result is not None and result.status_code == 200
    assert result.url == TARGET
    assert result.final_url == TARGET
    assert len(transport.calls) == 2
    assert pin.calls[0][0] == ORIGINAL
    assert pin.calls[1][0] == TARGET


async def test_multi_hop_within_budget_reaches_the_final_hop():
    """Multi-hop: three redirects then a terminal answer, all within the hop budget."""
    result, transport, *_ = await _run_walk(
        [_redirect("/y"), _redirect("/z"), _redirect(THIRD.raw), _terminal(url=THIRD)],
        [_pinned(ORIGINAL), _pinned(TARGET), _pinned(THIRD), _pinned(THIRD)],
        store=_CookieStore(),
    )
    assert result is not None and result.status_code == 200
    assert result.url == THIRD
    assert result.final_url == THIRD
    assert len(transport.calls) == 4


async def test_self_loop_exhausts_the_hop_budget():
    """Self-loop: redirecting to itself advances until hop exhaustion, then refuses."""
    script = [_redirect(ORIGINAL.raw)] * 6
    with pytest.raises(httpx.TooManyRedirects):
        await _run_walk(script, [_pinned(ORIGINAL)] * 6, store=_CookieStore())


async def test_cyclic_chain_exhausts_the_hop_budget():
    """Cyclic: A hopping to B and back advances until hop exhaustion, then refuses."""
    script = [_redirect(TARGET.raw), _redirect(ORIGINAL.raw)] * 3
    with pytest.raises(httpx.TooManyRedirects):
        await _run_walk(script, [_pinned(ORIGINAL), _pinned(TARGET)] * 3, store=_CookieStore())


def _assert_terminal(
    result: HttpFetchResult | None,
    transport: ScriptedTransport,
    expected_url: NormalizedUrl,
    expected_calls: int,
) -> None:
    """One verdict: terminal 200 at the expected URL with the expected fetch count."""
    assert result is not None and result.status_code == 200
    assert result.url == expected_url
    assert result.final_url == expected_url
    assert len(transport.calls) == expected_calls


async def test_unified_walker_one_verdict_per_chain():
    """One chain yields one verdict: RedirectWalker is the single redirect-walk implementation --
    a multi-hop chain with deferred cookies produces exactly one terminal result. Each hop's
    Set-Cookie defers until the next pin admits, then plants on the origin that sent it."""
    result, transport, pin, store = await _run_walk(
        [
            _result(302, url=ORIGINAL, final_url=ORIGINAL, headers={"set-cookie": "hop=1"}, location="/y"),
            _result(302, url=TARGET, final_url=TARGET, headers={"set-cookie": "hop=2"}, location="/z"),
            _terminal(url=THIRD, headers={"set-cookie": "hop=3"}),
        ],
        [_pinned(ORIGINAL), _pinned(TARGET), _pinned(THIRD)],
    )
    _assert_terminal(result, transport, THIRD, 3)
    assert len(pin.calls) == 3
    assert len(store.calls) == 3
    assert store.calls[0][0] == ORIGINAL
    assert store.calls[0][1]["set-cookie"] == "hop=1"
    assert store.calls[1][0] == TARGET
    assert store.calls[1][1]["set-cookie"] == "hop=2"
    assert store.calls[2][0] == THIRD
    assert store.calls[2][1]["set-cookie"] == "hop=3"
