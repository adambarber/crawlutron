"""Manual redirect loop re-checks every hop against SSRF policy before the
logical URL reaches the proxy: ``follow_redirects=True`` once followed into
an internal target without re-checking.
"""

import httpx
import pytest

from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport import pinned_transport as pinned_mod
from crawlutron.transport.classification import classify_error_code
from tests._assertions import block_private_targets
from tests._fetcher_factories import MockResponse, run_default_http_fetcher, scripted_transport_client


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


async def test_redirect_to_unsafe_loopback_is_not_followed(monkeypatch):
    """A 302 to an internal (loopback) target must NOT be fetched.

    The walker re-admits each hop through NormalizedUrl, so the private-target
    policy refuses the loopback before the logical URL reaches the proxy.
    """
    block_private_targets(monkeypatch)
    requested, _seen = _scripted_pool(
        monkeypatch,
        [MockResponse(302, {"location": "http://127.0.0.1:9/secret"})],
    )

    with pytest.raises(ValueError):
        await run_default_http_fetcher("http://public.example/")

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
    result = await run_default_http_fetcher("http://public.example/")

    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"
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

    with pytest.raises(httpx.TooManyRedirects):
        await run_default_http_fetcher("http://public.example/")
    assert len(requested) == pinned_mod.MAX_REDIRECT_HOPS + 1
    assert classify_error_code(httpx.TooManyRedirects("loop")) == "too_many_redirects"
