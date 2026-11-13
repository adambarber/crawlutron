"""Executor shutdown runs off the event loop and is bounded, not a hang.

The shutdown coerces ``ThreadPoolExecutor.shutdown(wait=True)`` off the loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

import crawlutron.extraction.documents as documents

pytestmark = pytest.mark.real_sleeps


class _Parked:
    """One extraction worker parked on ``release``; ``held`` is its future."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.executor = documents.extraction_executor()

        def parked() -> None:
            self.started.set()
            self.release.wait(timeout=5)

        self.held = self.executor.submit(parked)

    def wait_started(self) -> None:
        """Block this thread until the parked worker is actually running."""
        assert self.started.wait(5), "the parked extraction never started"

    def unpark(self) -> None:
        """Release the parked worker and drop any queued future, leak-free."""
        self.release.set()
        self.held.cancel()


@pytest.mark.usefixtures("four_worker_selector_pool")
def test_shutdown_does_not_block_the_event_loop():
    """A running extraction pins one worker; the loop must stay live meanwhile."""
    worker = _Parked()
    loop_live_while_held = [False]
    shutdown_done: list[bool] = []

    async def armed() -> None:
        async def ping() -> None:
            await asyncio.sleep(0.02)
            loop_live_while_held[0] = not worker.release.is_set()
            worker.release.set()

        ping_task = asyncio.ensure_future(ping())
        timer = threading.Timer(0.5, worker.release.set)
        timer.start()
        try:
            # Shutdown blocks waiting on ``held`` until ``release``; if it
            # ran on the loop, ``ping`` could not finish before the release.
            await asyncio.wait_for(
                documents.shutdown_doc_extraction_executor(),
                timeout=5,
            )
            shutdown_done.append(True)
        finally:
            timer.cancel()
            worker.release.set()
        await ping_task

    asyncio.run(armed())
    worker.unpark()
    assert shutdown_done, "shutdown never completed"
    assert loop_live_while_held[0], (
        "the event loop stalled while the extraction executor shut down: "
        "shutdown must run off-loop, not block the loop awaiting workers"
    )


@pytest.mark.usefixtures("four_worker_selector_pool")
def test_shutdown_times_out_and_logs_an_executor_warning(monkeypatch, caplog):
    """A worker that never finishes cannot hang shutdown: the bound fires, a
    warning names the executor, and the call returns for the next shutdown step."""
    monkeypatch.setattr(documents, "EXECUTOR_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
    worker = _Parked()
    caplog.set_level(logging.WARNING, logger="crawlutron.extraction.documents")

    async def bounded() -> None:
        timer = threading.Timer(0.5, worker.release.set)
        timer.start()
        try:
            # The parked worker is never released before the (small) bound;
            # only the timeout can let shutdown return.
            await asyncio.wait_for(
                documents.shutdown_doc_extraction_executor(),
                timeout=5,
            )
        finally:
            timer.cancel()
            worker.release.set()

    worker.wait_started()
    asyncio.run(bounded())
    worker.unpark()
    assert any("extraction" in record.message for record in caplog.records), (
        "the bound fired but no warning named the executor"
    )
