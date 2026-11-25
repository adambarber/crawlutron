"""The pooled httpx path egresses through the loopback proxy, never a pinned IP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx

from crawlutron.runtime import process_state
from crawlutron.transport import egress_proxy as egress_mod
from crawlutron.transport import http_client_pool, pinned_dns, pooled_clients
from crawlutron.transport.egress_proxy import serve_proxy
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.pinned_transport import PinnedRequest
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

PUBLIC_IP = "93.184.216.34"
LOGICAL = "logical.example"
BODY = b"through-proxy"

Dial = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@asynccontextmanager
async def _origin(capture: list[bytes]) -> AsyncGenerator[int]:
    """One loopback HTTP origin recording the request it received."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        capture.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n" + BODY)
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


async def test_httpx_fetch_egresses_through_proxy_with_logical_identity(monkeypatch) -> None:
    """The pooled client CONNECTs/forwards via the proxy; the proxy pins and dials."""
    seen: list[tuple[str, int]] = []
    origin_capture: list[bytes] = []
    monkeypatch.setattr(pinned_dns, "resolve_and_pin_host", lambda url: (url.host, PUBLIC_IP))
    monkeypatch.setattr(egress_mod, "resolve_and_pin_host", lambda url: (url.host, PUBLIC_IP))
    process_state.reset_all_process_state()
    async with _origin(origin_capture) as origin_port:
        proxy = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, dial=_record_dial(origin_port, seen))
        try:
            monkeypatch.setattr(
                pooled_clients, "egress_proxy_url", lambda policy: f"http://127.0.0.1:{proxy.port}"
            )
            result = await default_http_fetcher(canonical(f"http://{LOGICAL}/page"), {}, 10)
        finally:
            await proxy.aclose()
            process_state.reset_all_process_state()
            await pooled_clients.drain_pending_client_closes()
    assert result.status_code == 200
    assert result.body == BODY
    assert seen == [(PUBLIC_IP, 80)]
    assert b"Host: " + LOGICAL.encode() in origin_capture[0]
    assert PUBLIC_IP.encode() not in origin_capture[0]


async def test_pooled_httpx_stream_hands_the_logical_url_to_httpx(monkeypatch) -> None:
    """The facade dials the logical URL; the IP rewrite stays with cffi/browser."""
    observed: list[httpx.Request] = []

    async def record(request: httpx.Request) -> None:
        observed.append(request)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    real_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        _ = kwargs
        return real_client(transport=httpx.MockTransport(respond), event_hooks={"request": [record]})

    monkeypatch.setattr(pooled_clients.httpx, "AsyncClient", factory)
    process_state.reset_all_process_state()
    try:
        facade = http_client_pool.get_async_http_client()
        pinned = PinnedRequest(
            connect_url=f"https://{PUBLIC_IP}/secret",
            headers={"Host": LOGICAL},
            url=canonical(f"https://{LOGICAL}/secret"),
        )
        async with facade.stream("GET", pinned):
            pass
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()
    assert observed[0].url.host == LOGICAL
    assert observed[0].url.host != PUBLIC_IP
