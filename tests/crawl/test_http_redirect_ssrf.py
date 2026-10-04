"""The manual redirect loop re-checks every hop against the SSRF policy.

The fetcher used to hand httpx ``follow_redirects=True``, so a 302 to an
internal target was connected to without ever being looked at. It now follows
redirects itself, calling the pin builder on each hop. These tests drive the
real pooled ``httpx.AsyncClient`` over ``httpx.MockTransport`` rather than a
stand-in for it: ``stream()`` is then the real async context manager, the
redirect chain's own bookkeeping runs in front of a real client, and a client
that was left on automatic following would show up as an extra recorded request
instead of only as a flag nobody set.
"""

import httpx
import pytest

from app.crawl.strategies import http_client_pool as pool_mod
from app.crawl.strategies import pinned_transport as pinned_mod
from tests._fetcher_factories import run_default_http_fetcher
from tests._transport_doubles import MockResponse, scripted_transport_client


def _scripted_pool(monkeypatch, responses):
    """Stand the pooled client down to `responses`; return what it was asked.

    ``(requested, seen)``: the real requests httpx built, in order, and the real
    responses it handed back. Both come from the client's own hooks, so what the
    assertions read is what the fetcher actually caused rather than what a
    double believes it was told.
    """
    requested: list[httpx.Request] = []
    seen: list[httpx.Response] = []
    client = scripted_transport_client(responses, observed_requests=requested, observed_responses=seen)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda *a, **k: client)
    return requested, seen


def _follows_itself(seen: list[httpx.Response]) -> bool:
    """Whether any response was reached by httpx's own redirect following.

    A response the fetcher's loop fetched has an empty ``history``; one httpx
    followed itself arrives with the earlier hop already recorded. This is the
    stand-in for the old ``follow_redirects is False`` flag assertion, and it is
    the stronger form: a flag the double chose to report could not catch a
    client left on automatic following, this can.
    """
    return any(response.history for response in seen)


def _allow_every_host():
    """A pin that rewrites nothing, so the loop's own hop budget is what ends it."""
    return lambda url, headers, *, policy: (url, dict(headers))


def _reject_loopback(url, headers, *, policy):
    """A pin that refuses an internal target, the way the real one does."""
    if "127.0.0.1" in url:
        raise ValueError(f"SSRF: blocked target {url}")
    return url, dict(headers)


async def test_redirect_to_unsafe_loopback_is_not_followed(monkeypatch):
    """A 302 to an internal (loopback) target must NOT be fetched.

    The original code used follow_redirects=True, so httpx would connect to
    the redirect target without re-checking it -> SSRF. The fix follows
    redirects manually and re-runs the pin rule (which validates the target
    against the SSRF blocklist) on every hop, so an unsafe hop raises.
    """
    requested, _seen = _scripted_pool(
        monkeypatch,
        [MockResponse(302, {"location": "http://127.0.0.1:9/secret"})],
    )

    with pytest.raises(ValueError):
        await run_default_http_fetcher("http://public.example/", pin=_reject_loopback)

    # Only the initial (safe) URL was fetched; the unsafe redirect target was
    # validated and rejected before any second request was issued. The pin
    # target is the value's canonical raw (root slash stripped by the
    # normalizer's standing law).
    assert [str(request.url) for request in requested] == ["http://public.example"]


async def test_redirect_to_safe_target_is_followed(monkeypatch):
    """A redirect to another safe public host IS followed (within hop budget)."""
    requested, seen = _scripted_pool(
        monkeypatch,
        [
            MockResponse(302, {"location": "http://other.example/"}),
            MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
        ],
    )
    # Treat every host as safe so the loop can follow.
    result = await run_default_http_fetcher("http://public.example/", pin=_allow_every_host())

    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"
    # Initial request + one followed redirect, the second one by the loop and
    # not by httpx: automatic following would have left the hop in history.
    assert [str(request.url) for request in requested] == [
        "http://public.example",
        "http://other.example",
    ]
    assert not _follows_itself(seen)


async def test_redirect_loop_is_bounded_by_hop_limit(monkeypatch):
    """The manual loop must terminate even on indefinite redirects."""
    requested, _seen = _scripted_pool(
        monkeypatch,
        [
            MockResponse(302, {"location": "http://a.example/"})
            for _ in range(pinned_mod._MAX_REDIRECT_HOPS + 1)
        ],
    )
    result = await run_default_http_fetcher("http://public.example/", pin=_allow_every_host())

    # Terminates at the hop cap and returns the last 302 rather than looping.
    assert result.status_code == 302
    assert len(requested) == pinned_mod._MAX_REDIRECT_HOPS + 1
