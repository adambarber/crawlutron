"""The proxy's dial marker is evidence only on plain-HTTP absolute-form responses."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END

from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.runtime import process_state
from crawlutron.service import webhooks
from crawlutron.transport import egress_proxy as egress_mod
from crawlutron.transport import http_client_pool, pinned_dns, pooled_cffi, pooled_clients
from crawlutron.transport.cffi_transport import CffiTransport
from crawlutron.transport.egress_proxy import serve_proxy
from crawlutron.transport.transport import FetchRequest
from safe_url.host_policy import HostPolicy
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

_MARKER = "x-crawlutron-egress-refusal"
_FAKE = "dial-failed"
_PUBLIC_IP = "93.184.216.34"
_LOGICAL = "logical.example"


def _mock_http_client(monkeypatch, answer) -> None:
    """Build the real pooled facade over an httpx MockTransport answer."""
    real_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(answer))

    monkeypatch.setattr(pooled_clients.httpx, "AsyncClient", factory)


def _cffi_response(headers: Mapping[str, str], body: bytes = b"origin-cffi"):
    """A curl_cffi-shaped streamed response answering one terminal body."""

    class _Response:
        status_code = 200

        def __init__(self) -> None:
            self.headers = headers
            self.queue: asyncio.Queue[object] = asyncio.Queue()
            self.queue.put_nowait(body)
            self.queue.put_nowait(STREAM_END)

        async def aclose(self) -> None:
            return None

    return _Response()


def _mock_cffi_session(monkeypatch, response) -> None:
    class _Session:
        async def get(self, *args: object, **kwargs: object):
            return response

    monkeypatch.setattr(http_client_pool, "get_cffi_session", lambda: _Session())


async def test_https_origin_marker_is_yielded_by_pooled_httpx(monkeypatch) -> None:
    """An HTTPS origin's marker is origin data, never a proxy ConnectError."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={_MARKER: _FAKE}, content=b"origin-httpx")

    _mock_http_client(monkeypatch, answer)
    process_state.reset_all_process_state()
    try:
        facade = http_client_pool.get_async_http_client()
        request = FetchRequest(headers={}, url=canonical(f"https://{_LOGICAL}/page"), timeout_seconds=5.0)
        async with facade.stream("GET", request) as response:
            status = response.status_code
            body = await response.aread()
            marker = response.headers.get(_MARKER)
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
    assert status == 200
    assert body == b"origin-httpx"
    assert marker == _FAKE
    assert len(seen) == 1


async def test_https_origin_marker_is_yielded_by_cffi(monkeypatch) -> None:
    """An HTTPS origin's marker must not trigger the httpx fallback."""
    _mock_cffi_session(monkeypatch, _cffi_response({_MARKER: _FAKE}))
    request = FetchRequest(headers={}, url=canonical(f"https://{_LOGICAL}/page"), timeout_seconds=5.0)
    result = await CffiTransport().fetch(request, max_bytes=1024)
    assert result.status_code == 200
    assert result.body == b"origin-cffi"


async def test_https_webhook_marker_is_delivered_once(monkeypatch) -> None:
    """A hostile HTTPS receiver cannot turn one signed POST into retry storms."""
    posts: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, headers={_MARKER: _FAKE}, content=b"{}")

    _mock_http_client(monkeypatch, answer)
    monkeypatch.setenv("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", "0")
    process_state.reset_all_process_state()
    try:
        await webhooks.post_webhook(
            WebhookTarget.admit("https://webhook.example.com/cb", host_policy=HostPolicy.unrestricted()),
            {"results": []},
        )
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
    assert len(posts) == 1, f"the signed payload was posted {len(posts)} times"


async def test_http_origin_marker_still_raises_connect_error(monkeypatch) -> None:
    """A plain-HTTP proxy refusal keeps its transport-error meaning."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, headers={_MARKER: _FAKE})

    _mock_http_client(monkeypatch, answer)
    process_state.reset_all_process_state()
    try:
        facade = http_client_pool.get_async_http_client()
        request = FetchRequest(headers={}, url=canonical(f"http://{_LOGICAL}/page"), timeout_seconds=5.0)
        with pytest.raises(httpx.ConnectError, match="egress proxy could not reach the pinned target"):
            async with facade.stream("GET", request):
                pass
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()


async def test_cffi_http_origin_marker_still_raises_request_exception(monkeypatch) -> None:
    """The cffi family keeps falling back on a plain-HTTP proxy refusal."""
    response = _cffi_response({_MARKER: _FAKE})
    response.status_code = 502
    _mock_cffi_session(monkeypatch, response)
    request = FetchRequest(headers={}, url=canonical(f"http://{_LOGICAL}/page"), timeout_seconds=5.0)
    with pytest.raises(RequestException, match="egress proxy could not reach the pinned target"):
        await CffiTransport().fetch(request, max_bytes=1024)


async def test_https_connect_failure_stays_a_transport_error(monkeypatch) -> None:
    """A genuine HTTPS dial refusal is still a CONNECT failure, never a 200 page."""

    async def refuse_dial(host: str, port: int):
        raise OSError(f"unreachable {host}:{port}")

    monkeypatch.setattr(pinned_dns, "resolve_and_pin_host", lambda url: (url.host, _PUBLIC_IP))
    monkeypatch.setattr(egress_mod, "resolve_and_pin_host", lambda url: (url.host, _PUBLIC_IP))
    process_state.reset_all_process_state()
    proxy = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, dial=refuse_dial)
    try:
        monkeypatch.setattr(
            pooled_cffi,
            "egress_proxy_url",
            lambda policy, settings=None: f"http://127.0.0.1:{proxy.port}",
        )
        request = FetchRequest(headers={}, url=canonical(f"https://{_LOGICAL}/page"), timeout_seconds=5.0)
        with pytest.raises(RequestException):
            await CffiTransport().fetch(request, max_bytes=1024)
    finally:
        await proxy.aclose()
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
