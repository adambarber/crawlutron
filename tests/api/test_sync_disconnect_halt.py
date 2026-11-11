"""Sync /v1/crawl must halt when the client disconnects: the 283-abandon case
ran pages to feed a result nobody would read. ``crawl`` races the engine call
against the disconnect signal and cancels, letting cancellation free the slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from crawlutron.api import crawl_endpoint

#: The ASGI scope a ``Request`` is built from.
_SCOPE = {"type": "http", "method": "POST", "path": "/v1/crawl", "headers": [], "query_string": b""}


class _MidFlightGone(Request):
    """Client that disconnects mid-crawl: connected for the first polls so the task
    really starts -- disconnecting on the first check would cancel a never-started
    task, which never runs its body at all.
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


async def test_handler_cancellation_cancels_the_crawl_task():
    """Uvicorn shutdown cancels the handler mid-poll; the crawl task must be
    cancelled too, not left running to hold its slot for a discarded result.
    """
    seen: list[asyncio.Task[None]] = []

    async def _unfinished_crawl() -> str:
        task = asyncio.current_task()
        assert task is not None
        seen.append(task)
        await asyncio.Event().wait()
        return "discarded"

    handler = asyncio.ensure_future(
        crawl_endpoint.await_or_client_gone(_unfinished_crawl(), _NeverGone(), poll_interval=0.001)
    )
    await asyncio.wait_for(_until(seen), timeout=5)
    handler.cancel()
    await _handler_cancelled(handler)
    crawl_task = seen[0]
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(crawl_task, timeout=5)
    assert crawl_task.cancelled()


def _until(seen: list[asyncio.Task[None]]) -> Awaitable[bool]:
    async def _poll() -> bool:
        while not seen:
            await asyncio.sleep(0.001)
        return True

    return _poll()


def _handler_cancelled(handler: asyncio.Task[object]) -> Awaitable[object]:
    async def _run() -> object:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handler, timeout=5)
        return None

    return _run()


async def test_handler_cancelled_before_crawl_starts_starts_nothing():
    """Cancel before the handler's first step: the crawl coroutine is never awaited
    (nothing runs, no slot held); the abandoned coroutine is closed for hygiene.
    """
    started = asyncio.Event()

    def _crawl_factory() -> Coroutine[str, None, str]:
        async def _crawl() -> str:
            started.set()
            return "discarded"

        return _crawl()

    coro = _crawl_factory()
    handler = asyncio.ensure_future(crawl_endpoint.await_or_client_gone(coro, _NeverGone(), poll_interval=5))
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(handler, timeout=5)
    coro.close()
    assert not started.is_set()


async def test_handler_cancelled_after_crawl_completes_leaves_result_untouched():
    """Crawl finished but the handler is stuck polling: cancelling the handler
    must not cancel the finished task -- its result stays retrievable.
    """
    seen: list[asyncio.Task[None]] = []
    crawl_done = asyncio.Event()
    poll_block = asyncio.Event()

    class _StuckPoller(Request):
        def __init__(self) -> None:
            super().__init__(_SCOPE)

        async def is_disconnected(self) -> bool:
            await poll_block.wait()
            return False

    async def _quick_crawl() -> str:
        task = asyncio.current_task()
        assert task is not None
        seen.append(task)
        await asyncio.sleep(0)
        crawl_done.set()
        return "kept"

    handler = asyncio.ensure_future(
        crawl_endpoint.await_or_client_gone(_quick_crawl(), _StuckPoller(), poll_interval=0.001)
    )
    await asyncio.wait_for(crawl_done.wait(), timeout=5)
    await asyncio.sleep(0.01)
    assert not handler.done()
    handler.cancel()
    await _handler_cancelled(handler)
    crawl_task = seen[0]
    await asyncio.wait_for(crawl_task, timeout=5)
    assert crawl_task.cancelled() is False
    assert crawl_task.result() == "kept"


async def test_connected_crawl_propagates_engine_errors():
    async def _failing_crawl():
        raise ValueError("engine exploded")

    with pytest.raises(ValueError, match="engine exploded"):
        await crawl_endpoint.await_or_client_gone(_failing_crawl(), _NeverGone(), poll_interval=0.001)
