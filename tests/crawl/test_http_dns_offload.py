"""The HTTP fetcher must offload blocking DNS resolution
to a worker thread so the event loop is never stalled by a resolver call."""

import threading

import httpx
import pytest

from app.crawl.strategies import pinned_transport as http_mod
from app.crawl.strategies.fetchers import default_http_fetcher
from app.validation import HostPolicy


def _thread_recording_spy(seen: dict, *, dead_port: bool = True):
    """A ``_build_pinned_request`` stand-in that records the thread it ran on.

    Both tests below assert one property -- the builder ran off the event-loop
    thread -- so the recorder is one thing they share. Only what the spy hands
    back differs: the fetcher test points the connection at a closed loopback
    port so the fetch fails fast without real network work, and the wrapper test
    echoes the caller's own URL back so it can assert the return value.
    """

    def spy(url, headers, *, policy):
        seen["thread"] = threading.current_thread()
        return ("http://127.0.0.1:9/" if dead_port else url), dict(headers)

    return spy


async def test_http_fetcher_offloads_build_pinned_request_to_thread(monkeypatch):
    """_build_pinned_request (which performs a blocking socket.getaddrinfo) must
    run in a worker thread, not the event loop thread."""
    seen: dict = {}
    monkeypatch.setattr(http_mod, "_build_pinned_request", _thread_recording_spy(seen))

    # The spy redirects the connection at a closed loopback port, so the fetch
    # reaches the transport and fails to connect. ConnectError rather than a
    # blind Exception: a DNS or policy failure would raise a different type, and
    # that difference is what makes this test about offloading at all.
    with pytest.raises(httpx.ConnectError):
        await default_http_fetcher(
            "http://example.com/",
            {},
            timeout_seconds=2,
            policy=HostPolicy.unrestricted(),
        )

    assert seen, "_build_pinned_request was never invoked"
    # The spy ran in a dedicated DNS worker thread, not the asyncio event loop.
    assert seen["thread"] is not threading.current_thread()


async def test_build_pinned_request_async_runs_off_loop(monkeypatch):
    """The async wrapper itself dispatches to a thread (sanity check that the
    executor path is wired)."""
    seen: dict = {}

    monkeypatch.setattr(http_mod, "_build_pinned_request", _thread_recording_spy(seen, dead_port=False))
    result = await http_mod._build_pinned_request_async(
        "http://example.com/",
        {},
        policy=HostPolicy.unrestricted(),
    )
    assert seen
    assert seen["thread"] is not threading.current_thread()
    assert result == ("http://example.com/", {})
