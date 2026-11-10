"""Pinned cffi hop: the one-shot session must stay alive until the terminal body
is fully consumed, then close exactly once on every exit path.

The doubles model curl_cffi's real AsyncSession.close() semantics: closing the
session tears down the curl multi and aborts the in-flight transfer, surfacing
``STREAM_END`` and losing anything the queue has not yet drained. A double whose
``close()`` leaves the stream draining would false-green this suite.
"""

from __future__ import annotations

import asyncio

import pytest
from curl_cffi.requests.exceptions import RequestException, SSLError
from curl_cffi.requests.models import STREAM_END

import crawlutron.transport.http_client_pool as http_client_pool
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.runtime import process_state
from crawlutron.transport.cffi_transport import CffiTransport
from crawlutron.transport.transport import FetchRequest

EXPECTED_BODY = b"the-entire-terminal-body-must-arrive-complete"


class _AbortingQueue:
    """A curl_cffi stream queue: chunks are produced until the transfer ends.

    ``abort()`` models :meth:`curl_cffi.requests.AsyncSession.close()` tearing
    down the curl multi: the in-flight transfer ends early, anything not yet
    drained is lost, and ``STREAM_END`` surfaces immediately. A double that
    ignores close timing would false-green this suite.
    """

    def __init__(self, chunks: list[object], wedge: asyncio.Event | None = None) -> None:
        self._chunks = list(chunks)
        self.aborted = False
        self._wedge = wedge

    async def get(self) -> object:
        await asyncio.sleep(0)
        if self.aborted:
            # Transfer aborted by session close: remaining body is truncated.
            return STREAM_END
        if self._chunks:
            return self._chunks.pop(0)
        if self._wedge is not None:
            # Mid-stream back-pressure: block until the wedge is released so a
            # cancel can land while the drain is waiting on the queue.
            await self._wedge.wait()
        # Natural end of a fully-produced stream.
        return STREAM_END

    def abort(self) -> None:
        self.aborted = True


class _TruncatingResponse:
    """curl_cffi stream response shape: status, headers, queue, aclose."""

    def __init__(self, chunks: list[object]) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.queue = _AbortingQueue(chunks)
        self.aclose_awaited = False
        self.aclose_error: BaseException | None = None

    async def aclose(self) -> None:
        self.aclose_awaited = True
        if self.aclose_error is not None:
            raise self.aclose_error


class _OneShotSession:
    """A one-shot pinned session whose close() aborts the in-flight stream."""

    def __init__(self, response: _TruncatingResponse, dial_error: BaseException | None = None) -> None:
        self.response = response
        self.close_count = 0
        self.dial_error = dial_error

    async def get(self, url: str, **kwargs: object) -> _TruncatingResponse:
        if self.dial_error is not None:
            raise self.dial_error
        return self.response

    async def close(self) -> None:
        self.close_count += 1
        self.response.queue.abort()


class _SharedSession:
    """The pooled shared session: unused on the pinned hop, must not be reached."""

    async def get(self, url: str, **kwargs: object) -> None:
        raise AssertionError("the shared session must not serve a pinned hop")

    async def close(self) -> None:
        return None


async def test_pinned_terminal_body_is_complete_not_truncated(monkeypatch, stub_builder) -> None:
    transport, request, session, response = _pinned_hop(
        monkeypatch, stub_builder, [b"the-entire-", b"terminal-", b"body-must-arrive-complete"]
    )

    result = await transport.fetch(request, max_bytes=1 << 20)

    assert result.status_code == 200
    assert result.body == EXPECTED_BODY, f"terminal body truncated: {result.body!r}"
    assert session.close_count == 1
    assert response.aclose_awaited


def _pinned_hop(
    monkeypatch,
    stub_builder,
    chunks: list[object],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    dial_error: BaseException | None = None,
    wedge: bool = False,
) -> tuple[CffiTransport, FetchRequest, _OneShotSession, _TruncatingResponse]:
    """Build one pinned hop end-to-end: stubbed shared session, monkeypatched
    one-shot builder, and a fetch request that resolves to a pinned session."""
    process_state.reset_all_process_state()
    stub_builder("cffi", lambda settings=None: _SharedSession())

    response = _TruncatingResponse(chunks)
    response.status_code = status_code
    response.headers = dict(headers or {})
    if wedge:
        response.queue._wedge = asyncio.Event()
    session = _OneShotSession(response, dial_error=dial_error)
    monkeypatch.setattr(http_client_pool, "build_pinned_cffi_session", lambda resolve_entry: session)

    transport = CffiTransport()
    request = FetchRequest(
        connect_url="https://example.com/",
        headers={},
        url=NormalizedUrl("https://example.com/", policy=HostPolicy.unrestricted()),
        timeout_seconds=10,
        resolve_entry=["example.com:443:127.0.0.1"],
    )
    return transport, request, session, response


async def test_pinned_redirect_closes_session_once(monkeypatch, stub_builder) -> None:
    """A redirect hop reads no body but must still close the one-shot session."""
    transport, request, session, response = _pinned_hop(
        monkeypatch, stub_builder, [], status_code=302, headers={"location": "https://example.com/next"}
    )

    result = await transport.fetch(request, max_bytes=1 << 16)

    assert result.status_code == 302
    assert result.headers.get("location") == "https://example.com/next"
    assert session.close_count == 1


async def test_pinned_declared_length_413_closes_session_once(monkeypatch, stub_builder) -> None:
    """A declared-length oversized hop returns 413 and closes the session once."""
    transport, request, session, response = _pinned_hop(
        monkeypatch, stub_builder, [], status_code=200, headers={"content-length": "999999"}
    )

    result = await transport.fetch(request, max_bytes=10)

    assert result.status_code == 413
    assert session.close_count == 1


async def test_pinned_body_too_large_closes_session_once(monkeypatch, stub_builder) -> None:
    """A capped terminal stream raises BodyTooLarge and closes the session once."""
    from crawlutron.safety.bounded_io import BodyTooLarge

    transport, request, session, response = _pinned_hop(monkeypatch, stub_builder, [b"x" * 200])

    try:
        await transport.fetch(request, max_bytes=50)
    except BodyTooLarge:
        pass
    else:  # pragma: no cover - the cap must breach
        raise AssertionError("BodyTooLarge was not raised over the capped stream")

    assert session.close_count == 1


async def test_pinned_ssl_error_closes_session_once(monkeypatch, stub_builder) -> None:
    """SSLError from the pinned dial closes the session once and propagates."""
    transport, request, session, response = _pinned_hop(
        monkeypatch,
        stub_builder,
        [EXPECTED_BODY],
        dial_error=SSLError("certificate verify failed"),
    )

    with pytest.raises(SSLError):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 1
    assert not response.aclose_awaited


async def test_pinned_dial_failure_closes_session_once(monkeypatch, stub_builder) -> None:
    """Non-SSL dial failure closes the session once and propagates to the fallback."""
    transport, request, session, response = _pinned_hop(
        monkeypatch, stub_builder, [EXPECTED_BODY], dial_error=OSError("dial failed")
    )

    with pytest.raises(OSError):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 1


async def test_pinned_dial_cancelled_closes_session_once(monkeypatch, stub_builder) -> None:
    """CancelledError during the pinned dial closes the session once and propagates."""
    transport, request, session, response = _pinned_hop(
        monkeypatch,
        stub_builder,
        [EXPECTED_BODY],
        dial_error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 1


async def test_pinned_request_exception_mid_stream_closes_session_once(monkeypatch, stub_builder) -> None:
    """A RequestException raised mid-stream closes the session once and propagates."""
    transport, request, session, response = _pinned_hop(
        monkeypatch, stub_builder, [b"partial", RequestException("stream broke")]
    )

    with pytest.raises(RequestException):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 1


async def test_pinned_cancelled_mid_stream_closes_session_once(monkeypatch, stub_builder) -> None:
    """CancelledError mid-stream closes the session once and CancelledError propagates."""
    transport, request, session, response = _pinned_hop(monkeypatch, stub_builder, [b"partial"], wedge=True)

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

    assert session.close_count == 1


async def test_pinned_unexpected_error_closes_session_once(monkeypatch, stub_builder) -> None:
    """An unexpected error (aclose failure) after terminal drain closes once and propagates."""
    transport, request, session, response = _pinned_hop(monkeypatch, stub_builder, [EXPECTED_BODY])
    response.aclose_error = RuntimeError("unexpected close failure")

    with pytest.raises(RuntimeError, match="unexpected close failure"):
        await transport.fetch(request, max_bytes=1 << 20)

    assert session.close_count == 1
