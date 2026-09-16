"""Release-machinery honesty for the loop-bound pooled clients (D1, D2).

The shared pending-close queue is the durable half of the lifecycle: when the
cap displaces an entry, ``_release_dropped_client`` is the *only* thing standing
between that client and an unclosed TLS socket. The merged code documented it
as a synchronous close that "works from a sync context and from inside a dying
loop", but neither pooled kind can be closed that way:

  * ``httpx.AsyncClient`` publishes no ``close`` at all -- only ``aclose``,
  * ``curl_cffi``'s ``AsyncSession.close`` IS a coroutine function.

So the guard ``close is None or asyncio.iscoroutinefunction(close)`` returned
for both kinds and the displaced client was a pure reference drop. These tests
pin the real class shapes (so the docstring cannot drift back into fiction) and
pin the behavior the docstring now claims: a displaced client is actually
scheduled for close when a loop is available to run it.

D2 is the sibling hole on the eager path: ``_schedule_client_close`` guarded on
``getattr(client, "is_closed", True)``. A real ``AsyncSession`` has no
``is_closed`` attribute, so the default made every session look already closed
and the eager close never fired; the existing suite only passed because its
fake published ``is_closed = False`` for *both* kinds -- a lie about the cffi
class. The gate must come from the descriptor's own predicate instead.
"""

import asyncio
import inspect
import warnings

import httpx
import pytest
from curl_cffi.requests import AsyncSession

import app.crawl.strategies.http as http_mod

SLOTS = ("_DEFAULT_HTTP_CLIENT", "_DEFAULT_CFFI_SESSION")


@pytest.fixture(autouse=True)
def _clean_pooled_state(monkeypatch):
    for slot in SLOTS:
        monkeypatch.setattr(http_mod, slot, None, raising=False)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()
    yield
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()
    for slot in SLOTS:
        monkeypatch.setattr(http_mod, slot, None, raising=False)


class _RealShapedSession:
    """curl_cffi-shaped: a coroutine ``close`` and NO ``is_closed`` attribute."""

    def __init__(self) -> None:
        self.closed = False
        self._bound_loop = None

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


class _RealShapedHttpClient:
    """httpx-shaped: a coroutine ``aclose`` and no sync ``close``."""

    def __init__(self) -> None:
        self.is_closed = False
        self.closed = False
        self._bound_loop = None

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.is_closed = True
        self.closed = True


def test_real_pooled_classes_have_no_synchronous_close():
    """The premise of D1, pinned against the installed libraries.

    If either class ever gains a synchronous close, the reasoning behind
    ``_release_dropped_client`` has to be revisited -- this test is the trip
    wire that says so instead of a docstring that quietly stays wrong.
    """
    assert not hasattr(httpx.AsyncClient(), "close"), "httpx gained a sync close"
    assert inspect.iscoroutinefunction(AsyncSession.close), "cffi close is no longer async"
    assert not hasattr(AsyncSession, "is_closed"), "cffi now publishes is_closed"


def test_displaced_client_is_actually_released_when_a_loop_is_running(real_sleeps):
    """D1: the cap's displacement must reach a real close, not a reference drop.

    Needs real_sleeps: the await below must genuinely yield to the loop so the
    scheduled close actually runs during the scenario, not as an artifact of
    asyncio.run's teardown.
    """
    displaced = _RealShapedHttpClient()

    async def _scenario():
        http_mod._release_dropped_client(displaced)
        await asyncio.sleep(0.05)

    asyncio.run(_scenario())
    assert displaced.closed is True, (
        "_release_dropped_client released nothing: the displaced client was only dropped"
    )


def test_displaced_cffi_session_is_released_not_dropped(real_sleeps):
    """D1, cffi kind: the coroutine ``close`` is the only teardown it has.

    Needs real_sleeps for the same reason as its httpx twin above.
    """
    displaced = _RealShapedSession()

    async def _scenario():
        http_mod._release_dropped_client(displaced)
        await asyncio.sleep(0.05)

    asyncio.run(_scenario())
    assert displaced.closed is True, "a displaced AsyncSession was reference-dropped"


def test_displaced_client_with_no_running_loop_is_not_left_as_a_broken_coroutine():
    """Boundary: sync context must not create a never-awaited coroutine.

    The queue is the only owner in that case, so the reference is dropped --
    but doing it by building a coroutine nobody will await would trade a socket
    for a RuntimeWarning on every capped rebuild. ``_safe_aclose`` is asserted
    not to be called at all, which is the structural guarantee (no coroutine
    object exists to be unawaited), and warnings are escalated so an implicit
    event-loop fallback anywhere in the path fails loudly.
    """
    displaced = _RealShapedHttpClient()
    calls: list[object] = []
    real_aclose = http_mod._safe_aclose

    async def _spy(client):
        calls.append(client)
        return await real_aclose(client)

    http_mod._safe_aclose = _spy
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            http_mod._release_dropped_client(displaced)
    finally:
        http_mod._safe_aclose = real_aclose

    assert calls == [], "a teardown was scheduled with no loop able to run it"
    assert displaced.closed is False


def test_none_displacement_is_a_noop():
    """Inverse: an undisplaced enqueue must not blow up the release path."""
    http_mod._release_dropped_client(None)


def test_real_shaped_session_is_closed_eagerly_on_a_live_loop(monkeypatch, real_sleeps):
    """D2: the eager close must fire for a session with no ``is_closed``.

    Driven through the production ``get_cffi_session`` rebuild path with a
    curl_cffi-shaped object, so the guard cannot be satisfied by a fake
    inventing the attribute the real class does not have. The queue still holds
    the reference (that half is the durable safety net), but on a live loop the
    superseded session must already be closed by the time the request that
    triggered the rebuild finishes -- which is what the merged comment claims
    and the ``getattr(client, "is_closed", True)`` guard never delivered.
    """
    built: list[_RealShapedSession] = []

    def _build(*args, **kwargs):
        session = _RealShapedSession()
        session._bound_loop = asyncio.get_running_loop()
        built.append(session)
        return session

    monkeypatch.setattr(http_mod, "_build_cffi_session", _build)

    async def _scenario():
        first = http_mod.get_cffi_session()
        # Stand in for "this call arrived from a different loop".
        foreign = asyncio.new_event_loop()
        try:
            first._bound_loop = foreign
            second = http_mod.get_cffi_session()
            await asyncio.sleep(0.05)  # let a scheduled close run on this loop
        finally:
            foreign.close()
        return first, second

    first, second = asyncio.run(_scenario())
    assert second is not first
    assert first.closed is True, (
        "get_cffi_session superseded a real-shaped AsyncSession without closing "
        "it on the live loop -- the eager close never fires because the reaper "
        'reads a missing is_closed attribute as "already closed"'
    )


def test_httpx_kind_keeps_its_eager_close_after_the_gate_moves(monkeypatch, real_sleeps):
    """Inverse: moving the gate to the descriptor predicate must not cost the
    pooled httpx client the eager close it already had.
    """
    built: list[_RealShapedHttpClient] = []

    def _build(*args, **kwargs):
        client = _RealShapedHttpClient()
        client._bound_loop = asyncio.get_running_loop()
        built.append(client)
        return client

    monkeypatch.setattr(http_mod, "_build_async_client", _build)

    async def _scenario():
        first = http_mod.get_async_http_client()
        foreign = asyncio.new_event_loop()
        try:
            first._bound_loop = foreign
            second = http_mod.get_async_http_client()
            await asyncio.sleep(0.05)
        finally:
            foreign.close()
        return first, second

    first, second = asyncio.run(_scenario())
    assert second is not first
    assert first.closed is True, "the pooled httpx client lost its eager close"


def test_already_closed_pooled_client_is_not_re_closed_on_the_live_loop(monkeypatch, real_sleeps):
    """Boundary: the descriptor predicate still recognises a closed client.

    A client the caller reports as closed owns no live sockets, so the rebuild
    must drop it quietly instead of scheduling a pointless teardown task. The
    assertion is an event, not a task-count diff: a close scheduled on the
    loop may already have finished by the time the next statement runs, so
    "no new task visible" would prove nothing. ``closed`` is only ever set by
    the close coroutine itself, so observing it flip is positive evidence a
    teardown was scheduled.
    """

    def _build(*args, **kwargs):
        client = _RealShapedHttpClient()
        client._bound_loop = asyncio.get_running_loop()
        return client

    monkeypatch.setattr(http_mod, "_build_async_client", _build)

    async def _scenario():
        first = http_mod.get_async_http_client()
        first.is_closed = True  # someone else already closed it
        first.closed = False  # would flip only if a teardown were scheduled
        second = http_mod.get_async_http_client()
        await asyncio.sleep(0.05)
        return first, second

    first, second = asyncio.run(_scenario())
    assert second is not first, "a closed pooled client must be rebuilt"
    assert first.closed is False, "an already-closed client was scheduled for close"


def test_a_third_pooled_kind_is_not_left_out_by_the_httpx_attribute_shape(monkeypatch, real_sleeps):
    """D2's generalization: the closed-decision belongs to the descriptor.

    The merged guard probed for an attribute name (``is_closed``) instead of
    asking the descriptor that owns the kind. Any future pooled client that
    does not publish that attribute inherits the cffi bug exactly: treated as
    already closed, never closed eagerly. This third kind is deliberately
    neither httpx nor curl_cffi shaped.
    """
    import threading

    class _ThirdKind:
        def __init__(self) -> None:
            self.closed = False
            self._bound_loop = None

        async def aclose(self) -> None:
            await asyncio.sleep(0)
            self.closed = True

    monkeypatch.setattr(http_mod, "_THIRD_POOL_SLOT", None, raising=False)
    third = http_mod._LoopBoundPooledClient(
        slot="_THIRD_POOL_SLOT",
        build=lambda settings: _ThirdKind(),
        is_closed=lambda client: False,
        lock=threading.Lock(),
        consults_settings=False,
    )

    async def _scenario():
        first = third.get()
        foreign = asyncio.new_event_loop()
        try:
            first._bound_loop = foreign
            second = third.get()
            await asyncio.sleep(0.05)
        finally:
            foreign.close()
        return first, second

    first, second = asyncio.run(_scenario())
    assert second is not first
    assert first.closed is True, (
        "a pooled kind without an is_closed attribute was treated as already "
        "closed and never released eagerly"
    )
