"""discard_if_unowned_loop closes a loop-bound client on its owning loop,
and teardown hardening keeps the background close thread reliable."""

from __future__ import annotations

import asyncio
import threading
from typing import cast

import httpx
import pytest

from crawlutron.transport import pooled_clients as pc
from crawlutron.transport.pooled_clients import (
    PooledClient,
    bind_to_running_loop,
    discard_if_unowned_loop,
)


def _response(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_response))


def _slot_of(client: httpx.AsyncClient) -> dict[str, PooledClient | None]:
    """One test slot holding a real client, typed as the pooled protocol."""
    namespace: dict[str, httpx.AsyncClient | None] = {"_slot": client}
    return cast(dict[str, PooledClient | None], namespace)


def _release(client: httpx.AsyncClient) -> None:
    """Close through discard, then assert the client closed and the slot cleared."""
    namespace = _slot_of(client)
    discard_if_unowned_loop(client.aclose, "_slot", namespace)
    assert client.is_closed is True
    assert namespace["_slot"] is None


def test_discard_closes_client_with_no_current_loop(monkeypatch):
    """No current event loop: close on a fresh loop and release the slot."""
    client = _client()

    def _no_loop(*args: object) -> object:
        raise RuntimeError("no loop")

    monkeypatch.setattr(asyncio, "get_event_loop", _no_loop)
    _release(client)


def test_discard_closes_client_with_closed_current_loop(monkeypatch):
    """Closed current loop: close on a fresh loop, never on the dead one."""
    client = _client()
    dead = asyncio.new_event_loop()
    dead.close()
    monkeypatch.setattr(asyncio, "get_event_loop", lambda *args: dead)
    _release(client)


async def test_discard_closes_client_on_running_current_loop():
    """Running current loop: close on a background thread and release the slot."""
    client = _client()
    _release(client)


def test_discard_closes_client_on_the_owning_loop_running_elsewhere(monkeypatch):
    """Owning loop runs on another thread: close on it, never on a fresh loop."""
    client = _client()
    bound = asyncio.new_event_loop()
    started = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(bound)
        started.set()
        bound.run_forever()

    owner = threading.Thread(target=_run, daemon=True)
    owner.start()
    started.wait()

    async def _build() -> httpx.AsyncClient:
        bound_client = _client()
        bind_to_running_loop(bound_client)
        return bound_client

    client = asyncio.run_coroutine_threadsafe(_build(), bound).result(timeout=5)
    # With a bound loop the close must not touch new_event_loop or get_event_loop
    # at all: spin up neither, or the fallback branch ran instead of the bound one.
    monkeypatch.setattr(
        pc.asyncio,
        "get_event_loop",
        lambda *args: (_ for _ in ()).throw(RuntimeError("no loop")),
    )
    monkeypatch.setattr(
        pc.asyncio,
        "new_event_loop",
        lambda *args: (_ for _ in ()).throw(RuntimeError("a bound loop exists -- no fresh loop")),
    )
    namespace = _slot_of(client)
    try:
        discard_if_unowned_loop(client.aclose, "_slot", namespace)
        assert client.is_closed is True and namespace["_slot"] is None
    finally:
        bound.call_soon_threadsafe(bound.stop)
        owner.join(timeout=5)
        assert not owner.is_alive()
        bound.close()


def test_discard_closes_client_on_its_own_idle_loop():
    """Owning loop is open but idle: drive the close on it directly."""
    bound = asyncio.new_event_loop()

    async def _build() -> httpx.AsyncClient:
        client = _client()
        bind_to_running_loop(client)
        return client

    client = bound.run_until_complete(_build())
    _release(client)
    bound.close()


def test_discard_falls_back_when_owning_loop_is_closed(monkeypatch):
    """Owning loop closed: fall back to the cross-loop branches, fresh loop."""
    client = _client()
    bound = asyncio.new_event_loop()
    asyncio.set_event_loop(bound)
    bind_to_running_loop(client)
    bound.close()
    asyncio.set_event_loop(None)
    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
        lambda *args: (_ for _ in ()).throw(RuntimeError("no loop")),
    )
    _release(client)


async def test_discard_surfaces_close_failure_from_the_background_thread():
    """A BaseException raised by the close surfaces to the caller."""
    client = _client()
    namespace = _slot_of(client)

    async def _boom() -> object:
        raise KeyboardInterrupt("the close died on the background thread")

    with pytest.raises(KeyboardInterrupt, match="the close died on the background thread"):
        discard_if_unowned_loop(_boom, "_slot", namespace)


async def test_discard_raises_timeout_when_the_close_thread_stalls(monkeypatch):
    """A stalled close raises TimeoutError after done.wait(30) returns false."""
    client = _client()
    namespace = _slot_of(client)

    async def _never() -> object:
        while True:
            await asyncio.sleep(0.1)

    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    with pytest.raises(TimeoutError, match="within 30s"):
        discard_if_unowned_loop(_never, "_slot", namespace)


async def test_discard_close_thread_is_a_daemon(monkeypatch):
    """The background close thread is a daemon."""
    client = _client()
    captured: dict[str, bool] = {}

    class _SpyThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            captured["daemon"] = bool(kwargs.get("daemon", False))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pc.threading, "Thread", _SpyThread)

    namespace = _slot_of(client)
    assert not captured
    discard_if_unowned_loop(client.aclose, "_slot", namespace)
    assert captured["daemon"] is True
    assert client.is_closed is True and namespace["_slot"] is None
