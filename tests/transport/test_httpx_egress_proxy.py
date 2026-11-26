"""The pooled httpx path egresses through the loopback proxy, never a pinned IP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.runtime import process_state
from crawlutron.transport import egress_proxy as egress_mod
from crawlutron.transport import http_client_pool, pinned_dns, pooled_clients
from crawlutron.transport.egress_proxy import serve_proxy
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.transport import FetchRequest
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

PUBLIC_IP = "93.184.216.34"
LOGICAL = "logical.example"
BODY = b"through-proxy"
STANDARD_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n" + BODY

Dial = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@asynccontextmanager
async def _origin(capture: list[bytes], *, response: bytes = STANDARD_RESPONSE) -> AsyncGenerator[int]:
    """One loopback HTTP origin recording the request it received."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        capture.append(await reader.read(65536))
        writer.write(response)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _record_dial(port: int, seen: list[tuple[str, int]]) -> Dial:
    """Redirect the proxy's pinned dial to the loopback origin, recording it."""

    async def dial(host: str, target_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        seen.append((host, target_port))
        return await asyncio.open_connection("127.0.0.1", port)

    return dial


async def _fetch_through_proxy(
    monkeypatch, *, response: bytes = STANDARD_RESPONSE
) -> tuple[HttpFetchResult, list[tuple[str, int]], list[bytes]]:
    """Fetch one logical URL through a real loopback proxy over a loopback origin."""
    seen: list[tuple[str, int]] = []
    origin_capture: list[bytes] = []
    monkeypatch.setattr(pinned_dns, "resolve_and_pin_host", lambda url: (url.host, PUBLIC_IP))
    monkeypatch.setattr(egress_mod, "resolve_and_pin_host", lambda url: (url.host, PUBLIC_IP))
    process_state.reset_all_process_state()
    async with _origin(origin_capture, response=response) as origin_port:
        proxy = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, dial=_record_dial(origin_port, seen))
        try:
            monkeypatch.setattr(
                pooled_clients,
                "egress_proxy_url",
                lambda policy, settings=None: f"http://127.0.0.1:{proxy.port}",
            )
            result = await default_http_fetcher(canonical(f"http://{LOGICAL}/page"), {}, 10)
        finally:
            await proxy.aclose()
            process_state.reset_all_process_state()
            await pooled_clients.drain_pending_client_closes()
    return result, seen, origin_capture


async def test_httpx_fetch_egresses_through_proxy_with_logical_identity(monkeypatch) -> None:
    """The pooled client CONNECTs/forwards via the proxy; the proxy pins and dials."""
    result, seen, origin_capture = await _fetch_through_proxy(monkeypatch)
    assert result.status_code == 200
    assert result.body == BODY
    assert seen == [(PUBLIC_IP, 80)]
    assert b"Host: " + LOGICAL.encode() in origin_capture[0]
    assert PUBLIC_IP.encode() not in origin_capture[0]


async def test_admitted_http_origin_cannot_spoof_the_refusal_marker(monkeypatch) -> None:
    """An origin's injected refusal header never becomes a facade ConnectError."""
    spoofed = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\nx-crawlutron-egress-refusal: dial-failed\r\n\r\n" + BODY
    )
    result, _, _ = await _fetch_through_proxy(monkeypatch, response=spoofed)
    assert result.status_code == 200
    assert result.body == BODY


async def test_oversized_spoofed_header_block_is_failed_closed(monkeypatch) -> None:
    """An over-cap origin head carrying the refusal marker is refused, not forwarded."""
    oversized = (
        b"HTTP/1.1 200 OK\r\nx-crawlutron-egress-refusal: dial-failed\r\nX-Big: "
        + b"A" * 200000
        + b"\r\nContent-Length: 13\r\n\r\n"
        + BODY
    )
    result, _, _ = await _fetch_through_proxy(monkeypatch, response=oversized)
    assert result.status_code == 502
    assert "x-crawlutron-egress-refusal" not in result.headers
    assert result.body == b""


async def test_pooled_httpx_stream_hands_the_logical_url_to_httpx(monkeypatch) -> None:
    """The facade dials the logical URL; the IP rewrite stays with cffi/browser."""
    observed: dict[str, object] = {}
    requests: list[httpx.Request] = []

    async def record(request: httpx.Request) -> None:
        requests.append(request)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    real_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        observed.update(kwargs)
        return real_client(transport=httpx.MockTransport(respond), event_hooks={"request": [record]})

    monkeypatch.setattr(pooled_clients.httpx, "AsyncClient", factory)
    process_state.reset_all_process_state()
    try:
        facade = http_client_pool.get_async_http_client()
        request = FetchRequest(
            headers={"Host": LOGICAL},
            url=canonical(f"https://{LOGICAL}/secret"),
            timeout_seconds=5.0,
        )
        async with facade.stream("GET", request):
            pass
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
    proxy_url = observed["proxy"]
    assert isinstance(proxy_url, str) and proxy_url.startswith("http://127.0.0.1:")
    assert observed["trust_env"] is False
    assert requests[0].url.host == LOGICAL
    assert requests[0].url.host != PUBLIC_IP


def test_real_egress_proxy_url_starts_and_resets() -> None:
    """The sync seam reuses a live listener and drops it on reset."""
    from urllib.parse import urlsplit

    from crawlutron.transport import egress_proxy_runtime as runtime

    process_state.reset_all_process_state()
    try:
        url = runtime.egress_proxy_url(UNRESTRICTED_HOST_POLICY)
        parts = urlsplit(url)
        assert parts.scheme == "http" and parts.hostname == "127.0.0.1"
        assert runtime.has_egress_proxy() is True
        assert runtime.egress_proxy_url(UNRESTRICTED_HOST_POLICY) == url
    finally:
        process_state.reset_all_process_state()
    assert runtime.has_egress_proxy() is False
