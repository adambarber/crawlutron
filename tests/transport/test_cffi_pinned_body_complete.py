"""Proxied cffi hop: the shared session stays open across hops and the terminal body arrives whole.

The doubles model curl_cffi's AsyncSession: closing it would abort an in-flight transfer, so a
hop that closes the shared session would truncate the body and cross-tenant-replay cookies.
"""

from __future__ import annotations

import asyncio

import pytest
from curl_cffi.requests.exceptions import RequestException, SSLError
from curl_cffi.requests.models import STREAM_END

from crawlutron.runtime import process_state
from crawlutron.transport.cffi_transport import CffiTransport
from crawlutron.transport.transport import FetchRequest
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

EXPECTED_BODY = b"the-entire-terminal-body-must-arrive-complete"


class _WedgeableQueue:
    """A curl_cffi stream queue: scripted chunks, then an optional mid-stream wedge."""

    def __init__(self, chunks: list[object], wedge: asyncio.Event | None = None) -> None:
        self._chunks = list(chunks)
        self._wedge = wedge

    async def get(self) -> object:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        if self._wedge is not None:
            await self._wedge.wait()
        return STREAM_END


class _StreamResponse:
    """curl_cffi stream response shape: status, headers, queue, aclose."""

    def __init__(self, chunks: list[object]) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.queue = _WedgeableQueue(chunks)
        self.aclose_awaited = False
        self.aclose_error: BaseException | None = None

    async def aclose(self) -> None:
        self.aclose_awaited = True
        if self.aclose_error is not None:
            raise self.aclose_error


class _SharedSession:
    """The pooled shared session: one per loop, never closed by an individual hop."""

    def __init__(self, response: _StreamResponse, dial_error: BaseException | None = None) -> None:
        self.response = response
        self.close_count = 0
        self.dial_error = dial_error

    async def get(self, url: str, **kwargs: object) -> _StreamResponse:
        if self.dial_error is not None:
            raise self.dial_error
        return self.response

    async def close(self) -> None:
        self.close_count += 1


def _shared_hop(
    monkeypatch,
    stub_builder,
    chunks: list[object],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    dial_error: BaseException | None = None,
    wedge: bool = False,
) -> tuple[CffiTransport, FetchRequest, _SharedSession, _StreamResponse]:
    """Build one proxied hop end-to-end with a stubbed shared session."""
    process_state.reset_all_process_state()
    response = _StreamResponse(chunks)
    response.status_code = status_code
    response.headers = dict(headers or {})
    if wedge:
        response.queue._wedge = asyncio.Event()
    session = _SharedSession(response, dial_error=dial_error)
    stub_builder("cffi", lambda settings=None: session)

    transport = CffiTransport()
    request = FetchRequest(
        headers={},
        url=NormalizedUrl("https://example.com/", policy=HostPolicy.unrestricted()),
        timeout_seconds=10,
    )
    return transport, request, session, response


async def test_shared_terminal_body_is_complete_not_truncated(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _shared_hop(
        monkeypatch, stub_builder, [b"the-entire-", b"terminal-", b"body-must-arrive-complete"]
    )

    result = await transport.fetch(request, max_bytes=1 << 20)

    assert result.status_code == 200
    assert result.body == EXPECTED_BODY, f"terminal body truncated: {result.body!r}"
    assert session.close_count == 0
    assert response.aclose_awaited


async def test_shared_redirect_closes_response_not_session(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _shared_hop(
        monkeypatch, stub_builder, [], status_code=302, headers={"location": "https://example.com/next"}
    )

    result = await transport.fetch(request, max_bytes=1 << 16)

    assert result.status_code == 302
    assert result.headers.get("location") == "https://example.com/next"
    assert session.close_count == 0
    assert response.aclose_awaited


async def test_shared_declared_length_413_closes_response_not_session(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _shared_hop(
        monkeypatch, stub_builder, [], status_code=200, headers={"content-length": "999999"}
    )

    result = await transport.fetch(request, max_bytes=10)

    assert result.status_code == 413
    assert session.close_count == 0
    assert response.aclose_awaited


async def test_shared_body_too_large_keeps_session_open(monkeypatch, stub_builder) -> None:
    from crawlutron.safety.bounded_io import BodyTooLarge

    transport, request, session, _response = _shared_hop(monkeypatch, stub_builder, [b"x" * 200])

    try:
        await transport.fetch(request, max_bytes=50)
    except BodyTooLarge:
        pass
    else:  # pragma: no cover - the cap must breach
        raise AssertionError("BodyTooLarge was not raised over the capped stream")

    assert session.close_count == 0


async def test_shared_ssl_error_propagates_keeping_session_open(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _shared_hop(
        monkeypatch, stub_builder, [EXPECTED_BODY], dial_error=SSLError("certificate verify failed")
    )

    with pytest.raises(SSLError):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 0
    assert not response.aclose_awaited


async def test_shared_dial_failure_propagates_keeping_session_open(monkeypatch, stub_builder) -> None:
    transport, request, session, _response = _shared_hop(
        monkeypatch, stub_builder, [EXPECTED_BODY], dial_error=OSError("dial failed")
    )

    with pytest.raises(OSError):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 0


async def test_shared_dial_cancelled_propagates_keeping_session_open(monkeypatch, stub_builder) -> None:
    transport, request, session, _response = _shared_hop(
        monkeypatch, stub_builder, [EXPECTED_BODY], dial_error=asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 0


async def test_shared_request_exception_mid_stream_propagates(monkeypatch, stub_builder) -> None:
    transport, request, session, _response = _shared_hop(
        monkeypatch, stub_builder, [b"partial", RequestException("stream broke")]
    )

    with pytest.raises(RequestException):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 0


async def test_shared_cancelled_mid_stream_propagates(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _shared_hop(monkeypatch, stub_builder, [b"partial"], wedge=True)

    task = asyncio.create_task(transport.fetch(request, max_bytes=1 << 20))
    wedged = False
    for _ in range(200):
        if not response.queue._chunks:
            await asyncio.sleep(0)
            wedged = True
            break
        await asyncio.sleep(0)
    assert wedged, "drain never consumed the first chunk"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.close_count == 0


async def test_shared_unexpected_error_propagates_keeping_session_open(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _shared_hop(monkeypatch, stub_builder, [EXPECTED_BODY])
    response.aclose_error = RuntimeError("unexpected close failure")

    with pytest.raises(RuntimeError, match="unexpected close failure"):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 0
