"""The pooled curl_cffi path egresses through the loopback proxy, never a pinned IP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.runtime import process_state
from crawlutron.transport import egress_proxy as egress_mod
from crawlutron.transport import pinned_dns, pooled_cffi, pooled_clients
from crawlutron.transport.egress_proxy import serve_proxy
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.vocabulary.strategies import CFFI_IMPERSONATE_TARGET
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

PUBLIC_IP = "93.184.216.34"
LOGICAL = "logical.example"
BODY = b"through-proxy-cffi"
STANDARD_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(BODY)).encode() + b"\r\n\r\n" + BODY

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
                pooled_cffi, "egress_proxy_url", lambda policy: f"http://127.0.0.1:{proxy.port}"
            )
            result = await default_http_fetcher(
                canonical(f"http://{LOGICAL}/page"), {}, 10, 1024, impersonate=True
            )
        finally:
            await proxy.aclose()
            process_state.reset_all_process_state()
            await pooled_clients.drain_pending_client_closes()
    return result, seen, origin_capture


async def test_cffi_fetch_egresses_through_proxy_with_logical_identity(monkeypatch) -> None:
    """The cffi session forwards via the proxy; the proxy pins and dials the IP."""
    result, seen, origin_capture = await _fetch_through_proxy(monkeypatch)
    assert result.status_code == 200
    assert result.body == BODY
    assert seen == [(PUBLIC_IP, 80)]
    assert b"Host: " + LOGICAL.encode() in origin_capture[0]
    assert PUBLIC_IP.encode() not in origin_capture[0]


class _ConstructionProbe:
    """A pooled-client stand-in that records session-construction kwargs."""

    def __init__(self) -> None:
        self._bound_loop: object = None

    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


async def test_pooled_cffi_session_carries_proxy_and_refuses_ambient_env(monkeypatch) -> None:
    """The shared session is born with the loopback proxy and trust_env disabled."""
    observed: dict[str, object] = {}

    def factory(**kwargs: object) -> _ConstructionProbe:
        observed.update(kwargs)
        return _ConstructionProbe()

    monkeypatch.setattr(pooled_cffi.cffi_requests, "AsyncSession", factory)
    process_state.reset_all_process_state()
    try:
        assert pooled_cffi.get_cffi_session() is not None
    finally:
        process_state.reset_all_process_state()
        await pooled_clients.drain_pending_client_closes()

    proxy_url = observed["proxy"]
    assert isinstance(proxy_url, str) and proxy_url.startswith("http://127.0.0.1:")
    assert observed["trust_env"] is False
    assert observed["impersonate"] == CFFI_IMPERSONATE_TARGET
    assert observed["discard_cookies"] is True
    assert "curl_options" not in observed
