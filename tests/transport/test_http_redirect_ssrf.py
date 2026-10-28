"""The manual redirect loop re-checks every hop against the SSRF policy.

``follow_redirects=True`` once connected to an internal target without
re-check; it now follows itself, re-pinning per hop on the real client.
"""

import httpx
import pytest

from app.transport import http_client_pool as pool_mod
from app.transport import pinned_transport as pinned_mod
from app.transport.classification import classify_error_code
from tests._fetcher_factories import run_default_http_fetcher
from tests._transport_doubles import MockResponse, scripted_transport_client


def _scripted_pool(monkeypatch, responses):
    """Stand the pooled client down to `responses`; return what it was asked.

    ``(requested, seen)`` come from the client's own hooks, so the assertions
    read what the fetcher actually caused, not what a double was told.
    """
    requested: list[httpx.Request] = []
    seen: list[httpx.Response] = []
    client = scripted_transport_client(responses, observed_requests=requested, observed_responses=seen)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda *a, **k: client)
    return requested, seen


def _follows_itself(seen: list[httpx.Response]) -> bool:
    """Whether any response was reached by httpx's own redirect following.

    A hop the fetcher's loop fetched has empty ``history``; automatic
    following arrives with the earlier hop already recorded.
    """
    return any(response.history for response in seen)


def _allow_every_host():
    """A pin that rewrites nothing, so the loop's own hop budget is what ends it."""
    return lambda url, headers: (url.raw, dict(headers))


def _reject_loopback(url, headers):
    """A pin that refuses an internal target, the way the real one does."""
    if "127.0.0.1" in url.raw:
        raise ValueError(f"SSRF: blocked target {url.raw}")
    return url.raw, dict(headers)


async def test_redirect_to_unsafe_loopback_is_not_followed(monkeypatch):
    """A 302 to an internal (loopback) target must NOT be fetched.

    ``follow_redirects=True`` would connect to the target without re-checking;
    manual following re-runs the pin rule per hop, so an unsafe hop raises.
    """
    requested, _seen = _scripted_pool(
        monkeypatch,
        [MockResponse(302, {"location": "http://127.0.0.1:9/secret"})],
    )

    with pytest.raises(ValueError):
        await run_default_http_fetcher("http://public.example/", pin=_reject_loopback)

    # The pin target is the value's canonical raw; #520 keeps the root slash on it.
    assert [str(request.url) for request in requested] == ["http://public.example/"]


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
    # Initial request + one followed redirect, the second one by the loop and not by httpx: automatic
    # following would have left the hop in history.
    assert [str(request.url) for request in requested] == [
        "http://public.example/",
        "http://other.example/",
    ]
    assert not _follows_itself(seen)


async def test_redirect_loop_is_bounded_by_hop_limit(monkeypatch):
    """The manual loop must terminate even on indefinite redirects."""
    requested, _seen = _scripted_pool(
        monkeypatch,
        [
            MockResponse(302, {"location": "http://a.example/"})
            for _ in range(pinned_mod.MAX_REDIRECT_HOPS + 1)
        ],
    )

    # Terminates at the hop cap by surfacing the exhaustion, not by reading the
    # last 302's body back as a rendered page (#217).
    with pytest.raises(httpx.TooManyRedirects):
        await run_default_http_fetcher("http://public.example/", pin=_allow_every_host())
    assert len(requested) == pinned_mod.MAX_REDIRECT_HOPS + 1
    assert classify_error_code(httpx.TooManyRedirects("loop")) == "too_many_redirects"
