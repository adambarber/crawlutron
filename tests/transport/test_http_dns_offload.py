"""The app no longer resolves target hostnames: the proxy owns target DNS and
the only dial, so no fetch path offloads a blocking resolver call.
"""

from __future__ import annotations

import httpx

import crawlutron.transport.pinned_dns as pinned_dns
from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.redirect_walker import RedirectWalker
from crawlutron.transport.transport import FetchRequest
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import (
    MockResponse,
    ScriptedTransport,
    _void_store_cookies,
    scripted_transport_client,
)
from tests._policy_factories import canonical


def _forbid_app_dns(monkeypatch, calls: list[str]):
    async def async_resolve(work, *, timeout_seconds):
        calls.append("async")
        raise AssertionError("app-side target DNS in the fetch path")

    def sync_resolve(url):
        calls.append("sync")
        raise AssertionError("app-side target DNS in the fetch path")

    monkeypatch.setattr(pinned_dns, "bounded_thread_resolve", async_resolve)
    monkeypatch.setattr(pinned_dns, "resolve_and_pin_host", sync_resolve)


async def test_http_fetcher_never_runs_app_side_dns(monkeypatch):
    calls: list[str] = []
    _forbid_app_dns(monkeypatch, calls)
    requested: list[httpx.Request] = []
    client = scripted_transport_client(
        [MockResponse(200, {"content-type": "text/html"}, b"ok")],
        observed_requests=requested,
    )
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)

    result = await default_http_fetcher(canonical("http://example.com/"), {}, 10)

    assert result is not None and result.status_code == 200
    assert calls == []
    assert [str(request.url) for request in requested] == ["http://example.com/"]


async def test_redirect_walk_never_runs_app_side_dns(monkeypatch):
    calls: list[str] = []
    _forbid_app_dns(monkeypatch, calls)
    transport = ScriptedTransport(
        [make_fetch(status=200, body=b"ok", url="http://example.com/", final_url="http://example.com/")]
    )

    await RedirectWalker(transport, _void_store_cookies).walk(canonical("http://example.com/"), {}, 10)

    assert calls == []
    request = transport.calls[0][0]
    assert isinstance(request, FetchRequest)
    assert request.url == canonical("http://example.com/")
    assert not hasattr(request, "connect_url")
