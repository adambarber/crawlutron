"""A reset called while a loop runs must not drive an idle owning loop here."""

from __future__ import annotations

import asyncio
import gc
import warnings
from collections.abc import Iterator
from typing import cast

import pytest

from crawlutron.runtime import process_state
from crawlutron.transport import pooled_clients
from crawlutron.transport.pooled_client_release import (
    PENDING_CLIENT_CLOSE,
    PooledClient,
    discard_if_unowned_loop,
)
from tests._assertions import accepted_crawl, poll_crawl_completion
from tests._async_harnesses import crawl_api as _crawl_api

crawl_api = _crawl_api


@pytest.fixture
def idle_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """An open, never-running loop for the harness rebind."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class _ClosingClient:
    """A pooled-client shape whose close is observable and loop-free."""

    def __init__(self) -> None:
        self.closed = False
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    async def aclose(self) -> None:
        self.closed = True

    async def close(self) -> None:
        await self.aclose()


def _rebind_and_reset(facade, foreign: asyncio.AbstractEventLoop) -> list[str]:
    """Rebind the pooled facade, reset, and return the GC-time warning texts."""
    facade._bound_loop = foreign
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        process_state.reset_all_process_state()
        gc.collect()
    return [str(w.message) for w in caught]


async def test_reset_with_a_crawl_and_idle_bound_loop_leaves_no_orphans(crawl_api, idle_loop):
    """The real-crawl reset must close the client, not strand its close coroutine."""
    facade = pooled_clients.get_async_http_client()
    accepted = await accepted_crawl(crawl_api.client, crawl_api.payload(async_mode=True))
    await poll_crawl_completion(crawl_api.client, str(accepted["request_id"]))

    messages = _rebind_and_reset(facade, idle_loop)
    assert not [message for message in messages if "never awaited" in message]
    assert pooled_clients.http_singleton().current is None
    assert not PENDING_CLIENT_CLOSE


def test_reset_without_a_crawl_keeps_the_idle_bound_loop_clean(idle_loop):
    """The control: no crawl, no running loop, no orphan close."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pooled_clients.get_async_http_client()
        messages = _rebind_and_reset(pooled_clients.http_singleton().current, idle_loop)
    finally:
        asyncio.set_event_loop(None)
        if not loop.is_closed():
            loop.close()
    assert not [message for message in messages if "never awaited" in message]
    assert pooled_clients.http_singleton().current is None
    assert not PENDING_CLIENT_CLOSE


async def test_discard_on_idle_bound_loop_while_this_loop_runs_closes_and_clears(idle_loop):
    """The release door must close and clear, never drive the idle loop here.

    Mutant killed: removing the running-loop guard restores the raw
    ``run_until_complete`` that raises before the close coroutine starts.
    """
    client = _ClosingClient()
    client._bound_loop = idle_loop
    namespace: dict[str, PooledClient | None] = {"_slot": cast(PooledClient, client)}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        discard_if_unowned_loop(client.aclose, "_slot", namespace)
        gc.collect()
    assert client.closed is True
    assert namespace["_slot"] is None
    assert not [str(w.message) for w in caught if "never awaited" in str(w.message)]


@pytest.mark.parametrize("queue_depth", [0, 1, 3])
async def test_idle_bound_reset_drains_every_queued_close(queue_depth: int, idle_loop):
    """The boundary matrix: an empty, one, and many queued closes all drain."""
    facade = pooled_clients.get_async_http_client()
    for _ in range(queue_depth):
        PENDING_CLIENT_CLOSE.append(cast(PooledClient, _ClosingClient()))
    _rebind_and_reset(facade, idle_loop)
    assert pooled_clients.http_singleton().current is None
    assert not PENDING_CLIENT_CLOSE
