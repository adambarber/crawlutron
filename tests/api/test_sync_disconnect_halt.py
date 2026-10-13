"""Sync ``/v1/crawl`` must halt when the client disconnects.

A synchronous crawl runs to completion even when the caller has gone away (the
283-abandon case: the driver timed out, but the server kept crawling pages to
feed a result nobody would read). ``crawl`` therefore races the engine call
against the client's disconnect signal and cancels the crawl, letting the
engine's existing cancellation cleanup free the slot/browser.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import crawl_endpoint

#: The ASGI scope a ``Request`` is built from.
_SCOPE = {"type": "http", "method": "POST", "path": "/v1/crawl", "headers": [], "query_string": b""}


class _MidFlightGone(Request):
    """Client that disconnects mid-crawl (the 283-abandon shape).

    Reports connected for the first ``after_checks - 1`` polls so the crawl
    task actually starts, then disconnects. Disconnecting on the very first
    check would cancel a never-started task, which never runs its body at all.
    """

    def __init__(self, after_checks: int = 2) -> None:
        super().__init__(_SCOPE)
        self.after_checks = after_checks
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks >= self.after_checks


class _NeverGone(Request):
    """Connected client; must never trigger the halt."""

    def __init__(self) -> None:
        super().__init__(_SCOPE)

    async def is_disconnected(self) -> bool:
        return False


async def test_disconnect_cancels_the_running_crawl():
    cancelled = asyncio.Event()

    async def _slow_crawl():
        try:
            # Never resolves on its own: the disconnect poll is the only thing that can end this crawl, which
            # is exactly what the test asserts.
            await asyncio.Event().wait()
            return "done"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(HTTPException) as exc_info:
        await crawl_endpoint.await_or_client_gone(_slow_crawl(), _MidFlightGone(), poll_interval=0.001)
    assert exc_info.value.status_code == 499
    assert cancelled.is_set()


async def test_connected_crawl_returns_its_result():
    async def _fast_crawl():
        return "result-value"

    assert (
        await crawl_endpoint.await_or_client_gone(_fast_crawl(), _NeverGone(), poll_interval=0.001)
        == "result-value"
    )


async def test_connected_crawl_propagates_engine_errors():
    async def _failing_crawl():
        raise ValueError("engine exploded")

    with pytest.raises(ValueError, match="engine exploded"):
        await crawl_endpoint.await_or_client_gone(_failing_crawl(), _NeverGone(), poll_interval=0.001)
