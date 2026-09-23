"""Adversarial perf #1.1: the HTTP fetcher must offload blocking DNS resolution
to a worker thread so the event loop is never stalled by a resolver call."""

import threading

import pytest

from app.crawl.strategies import http as http_mod
from app.crawl.strategies.http import _default_http_fetcher
from app.validation import HostPolicy


async def test_http_fetcher_offloads_build_pinned_request_to_thread(monkeypatch):
    """_build_pinned_request (which performs a blocking socket.getaddrinfo) must
    run in a worker thread, not the event loop thread."""
    seen = {}

    def spy(url, headers, *, policy):
        seen["thread"] = threading.current_thread()
        # Point the connection at a closed loopback port so the fetch fails fast
        # without performing any real network work.
        return "http://127.0.0.1:9/", dict(headers)

    monkeypatch.setattr(http_mod, "_build_pinned_request", spy)

    with pytest.raises(Exception):
        await _default_http_fetcher(
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
    seen = {}

    def spy(url, headers, *, policy):
        seen["thread"] = threading.current_thread()
        return url, dict(headers)

    monkeypatch.setattr(http_mod, "_build_pinned_request", spy)
    result = await http_mod._build_pinned_request_async(
        "http://example.com/",
        {},
        policy=HostPolicy.unrestricted(),
    )
    assert seen
    assert seen["thread"] is not threading.current_thread()
    assert result == ("http://example.com/", {})
