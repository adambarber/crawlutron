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


def test_schedule_client_close_schedules_on_a_live_loop(real_sleeps):
    """The happy path still schedules the close and holds it to completion.

    ``real_sleeps`` is required: the assertion depends on the *scheduled* task
    actually running while we yield, which the autouse fast-sleep stub (no
    real yield) starves -- rule #1810.
    """

    async def _drive() -> None:
        client = _ClosingClient()
        http_mod._schedule_client_close(client, is_closed=lambda c: False)
        # Yield so the scheduled task runs; the inflight strong-reference set
        # keeps it alive until then.
        for _ in range(5):
            await asyncio.sleep(0)
        assert client.closed is True

    asyncio.run(_drive())
