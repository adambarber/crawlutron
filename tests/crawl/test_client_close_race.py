"""Close-race tests for best-effort client teardown scheduling (H5).

``_schedule_client_close`` swaps the shared ``_LoopBoundPooledClient`` from
synchronous call sites (client-creation and DNS-recovery paths). The scheduled
``aclose`` must never raise into those paths: the ``get_running_loop`` guard
only covers "no loop at all", but a loop that closes between the lookup and
``create_task`` still raises ``RuntimeError`` from the swap path. The race
window can't be won by sleeping, so the double is a closed loop handed back by
the lookup -- the exact state the guard must reject.
"""

from __future__ import annotations

import asyncio

from _timing_harnesses import await_until

from app.crawl.strategies import http_client_pool as http_mod


class _ClosingClient:
    """Async-closeable double that records whether its aclose ran."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_schedule_client_close_survives_a_closed_loop(monkeypatch):
    """A closed loop must be a silent no-op, never a raise into the swap path.

    Driven from inside a running loop with the lookup patched to hand back a
    closed one: without the guard the code proceeds to ``create_task`` on the
    dead loop and raises, which is exactly the swap-path crash under test.
    """
    dead = asyncio.new_event_loop()
    dead.close()
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: dead)

    async def _drive() -> None:
        client = _ClosingClient()
        http_mod._schedule_client_close(client, is_closed=lambda c: False)  # must not raise
        assert client.closed is False  # nothing was scheduled on a dead loop

    asyncio.run(_drive())


def test_schedule_client_close_schedules_on_a_live_loop():
    """The happy path still schedules the close and holds it to completion.

    The wait is on the close, not on a pause: the scheduled task sets
    ``closed``, so the loop is handed back until it has -- which is the
    observable. The inflight strong-reference set keeps the task alive until
    then, so a close scheduled on this loop cannot be collected before it runs.
    """

    async def _drive() -> None:
        client = _ClosingClient()
        http_mod._schedule_client_close(client, is_closed=lambda c: False)
        await await_until(
            lambda: client.closed,
            "the scheduled aclose never ran on a live loop",
        )
        assert client.closed is True

    asyncio.run(_drive())
