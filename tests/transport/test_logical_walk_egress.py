"""The logical request reaches the proxy with no app-side target resolution."""

from __future__ import annotations

import socket

import httpx
import pytest

from crawlutron.transport.http_client_pool import get_async_http_client
from crawlutron.transport.redirect_walker import RedirectWalker
from crawlutron.transport.transport import FetchRequest, FetchResult
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import MockResponse, scripted_transport_client

UNRESOLVABLE = "http://no-such-target.invalid/page"


class _RecordingTransport:
    """Capture the request a walk hands over, then answer with a terminal body."""

    def __init__(self) -> None:
        self.requests: list[FetchRequest] = []

    async def fetch(self, request: FetchRequest, *, max_bytes: int) -> FetchResult:
        self.requests.append(request)
        return make_fetch(
            url=request.url,
            final_url=request.url,
            headers={"content-type": "text/plain"},
            body=b"ok",
        )


async def _store_nothing(url: NormalizedUrl, headers) -> None:
    return None


async def test_walk_carries_logical_url_without_app_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable target is carried logically; no getaddrinfo in the app."""

    def refuse_resolution(*args, **kwargs):
        raise AssertionError("app-side target DNS must never run")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_resolution)
    url = NormalizedUrl(UNRESOLVABLE, policy=HostPolicy.unrestricted())
    transport = _RecordingTransport()
    result = await RedirectWalker(transport, _store_nothing).walk(url, {}, 5.0)
    assert result is not None and result.body == b"ok"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.url.raw == url.raw
    assert not hasattr(request, "connect_url")
    assert request.tls_hostname is None


async def test_pooled_facade_dials_the_logical_url(stub_builder) -> None:
    """The pooled facade hands the logical URL to httpx, never a resolved IP."""
    observed: list[httpx.Request] = []
    client = scripted_transport_client([MockResponse(200, content=b"ok")], observed_requests=observed)
    request = FetchRequest(
        headers={},
        url=NormalizedUrl(UNRESOLVABLE, policy=HostPolicy.unrestricted()),
        timeout_seconds=5.0,
    )
    stub_builder("http", lambda settings: client)
    pooled = get_async_http_client()
    async with pooled.stream("GET", request) as response:
        assert response.status_code == 200
    assert len(observed) == 1
    assert observed[0].url == httpx.URL(UNRESOLVABLE)
