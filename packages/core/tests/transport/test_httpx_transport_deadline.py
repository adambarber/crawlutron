"""End-to-end transfer deadline for the httpx fetch hop (issue #1127).

A drip-feeding server stays just under the per-read inactivity timeout, so
httpx's own timeout never trips; only the end-to-end deadline ends the hop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from crawlutron.transport.httpx_transport import HttpxTransport
from crawlutron.transport.transport import FetchRequest, FetchResult
from tests._policy_factories import canonical

_DRIP_INTERVAL = 0.25
_READ_TIMEOUT = 0.30
_DEADLINE_FACTOR = 8  # CI-load headroom on the asserted end-to-end bound
_WAIT_FOR_BOUND = 5.0  # outer guard; must exceed the asserted bound


@pytest.fixture(autouse=True)
def _allow_private_targets(monkeypatch):
    """Loopback targets are private by default; this hop dials 127.0.0.1."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")


async def _drip_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer with headers then drip one byte just under the read timeout forever."""
    await reader.read(1)
    writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 1000000\r\n\r\n")
    await writer.drain()
    try:
        while True:
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(_DRIP_INTERVAL)
    except OSError:
        pass  # the far side closed after the deadline; stop dripping
    finally:
        writer.close()


async def _quick_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A prompt complete response."""
    await reader.read(1)
    writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nab")
    await writer.drain()
    writer.close()


@pytest.mark.real_sleeps
class TestDripFeedEndToEndDeadline:
    """A drip feeder must not hold a hop past the end-to-end deadline."""

    async def _fetch(self, handler) -> FetchResult:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            request = FetchRequest(
                connect_url=f"http://127.0.0.1:{port}/",
                headers={"Host": "127.0.0.1"},
                url=canonical(f"http://127.0.0.1:{port}/"),
                timeout_seconds=_READ_TIMEOUT,
            )
            transport = HttpxTransport()

            async def probe():
                return await transport.fetch(request, max_bytes=1 << 20)

            return await asyncio.wait_for(probe(), timeout=_WAIT_FOR_BOUND)
        finally:
            server.close()
            await server.wait_closed()

    async def test_drip_feed_fails_within_end_to_end_deadline(self):
        """Drip stays under the read timeout; the hop still ends at the deadline."""
        started = time.monotonic()
        try:
            await self._fetch(_drip_handler)
        except TimeoutError:
            elapsed = time.monotonic() - started
            assert elapsed < _READ_TIMEOUT * _DEADLINE_FACTOR, (
                f"hop held for {elapsed:.2f}s: no end-to-end deadline"
            )
            return
        pytest.fail("fetch did not raise: the drip feeder held the hop past the deadline")

    async def test_fast_response_returns_within_deadline(self):
        """A prompt response still completes normally (no false positive)."""
        result = await self._fetch(_quick_handler)
        assert result.status_code == 200
