"""HTTP client pool: shared per-loop client/session lifecycle, supersession, close queue and scaling."""

import asyncio
import inspect
import warnings
from unittest.mock import MagicMock, patch

import httpx
import pytest
from curl_cffi.requests import AsyncSession

import app.crawl.strategies.http as http_mod
from app import settings as settings_mod
from app.crawl.strategies.http import get_async_http_client
from app.validation import HostPolicy


class TestAsyncHttpClientLifecycle:
    async def test_http_async_client_close(self):

        from app.crawl.strategies.http import close_async_http_client, get_async_http_client

        # Get a client, then close it
        async def test():
            get_async_http_client()
            await close_async_http_client()

        await test()


class TestPendingClientCloseCap:
    def test_pending_client_close_has_cap(self):
        from app.crawl.strategies import http as http_mod

        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_MAX")
        assert http_mod._PENDING_CLIENT_CLOSE_MAX >= 1
        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_LOCK")

    def test_pending_client_close_cap_drops_oldest_silently(self):
        """When the cap is reached, the oldest entry is dropped (reference released)
        rather than scheduled for close via ``asyncio.ensure_future``. The drop path
        must work without a running event loop (sync tests / shutdown paths) so
        any attempt to schedule a future here would either raise or schedule on
        the wrong loop, silently leaking the sockets (adversarial review
        F-pending-client-close).
        """
        from unittest.mock import MagicMock

        from app.crawl.strategies import http as http_mod

        # Pre-fill the pending list to the cap with mock clients.
        mocks = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.extend(mocks)
        # The fix must drop the oldest reference without calling
        # ``asyncio.ensure_future`` (which has no running loop in this context).
        oldest = mocks[0]
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            # Simulate the cap path
            assert len(http_mod._PENDING_CLIENT_CLOSE) >= http_mod._PENDING_CLIENT_CLOSE_MAX
            dropped = http_mod._PENDING_CLIENT_CLOSE.pop(0)
        assert dropped is oldest
        assert oldest not in http_mod._PENDING_CLIENT_CLOSE

    def test_pending_client_close_cap_calls_sync_close_on_dropped(self):
        sample = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
        for c in sample:
            c.close = MagicMock()
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.clear()
            for c in sample:
                http_mod._PENDING_CLIENT_CLOSE.append(c)
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            if len(http_mod._PENDING_CLIENT_CLOSE) >= http_mod._PENDING_CLIENT_CLOSE_MAX:
                stale = http_mod._PENDING_CLIENT_CLOSE.pop(0)
                stale.close()
        sample[0].close.assert_called_once()
        assert sample[0] not in http_mod._PENDING_CLIENT_CLOSE

    def test_pending_client_close_sync_close_failure_is_swallowed(self):
        class _BoomClient:
            def close(self):
                raise RuntimeError("already closed")

        boom = _BoomClient()
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.clear()
            for _ in range(http_mod._PENDING_CLIENT_CLOSE_MAX):
                http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
            http_mod._PENDING_CLIENT_CLOSE[0] = boom
            stale = http_mod._PENDING_CLIENT_CLOSE.pop(0)
            try:
                stale.close()
            except Exception:
                pass
            http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
        assert len(http_mod._PENDING_CLIENT_CLOSE) == http_mod._PENDING_CLIENT_CLOSE_MAX

    def test_pending_client_close_dedupe_and_cap_are_atomic(self):
        src_path = "app/crawl/strategies/http.py"
        with open(src_path) as f:
            source = f.read()
        assert "already_queued = old_client in _PENDING_CLIENT_CLOSE" in source
        assert "if not already_queued:" in source


@pytest.fixture(autouse=True)
def _clean_pooled_state(monkeypatch):
    """Start each test with no pooled client, no queued closes, no leftovers."""
    for slot in ("_DEFAULT_HTTP_CLIENT", "_DEFAULT_CFFI_SESSION"):
        monkeypatch.setattr(http_mod, slot, None, raising=False)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()
    yield
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.clear()
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None, raising=False)
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", None, raising=False)


class _PooledFake:
    """Minimal loop-bound client: records its bound loop and whether it closed.

    ``close`` yields to the loop first, like a real socket teardown, so a close
    scheduled on a dying loop cannot be relied on to finish -- that is exactly
    the hole the pending-close queue exists to cover.

    ``closer`` names the teardown attribute the real kind publishes, and the
    shape differences between the two kinds are part of what is being pinned:
    httpx exposes ``aclose`` plus an ``is_closed`` flag, while curl_cffi's
    ``AsyncSession`` exposes ``close`` and NO ``is_closed`` at all. The cffi fake
    must therefore not grow an ``is_closed`` -- doing so hides the fact that the
    shared reaper's ``getattr(client, "is_closed", True)`` guard silently treats
    every real session as already closed (D2). Each descriptor's own ``is_closed``
    predicate is what decides reuse-safety.
    """

    def __init__(self, closer: str, *, publishes_is_closed: bool) -> None:
        setattr(self, closer, self._closed)
        self.closed = False
        self._bound_loop = None
        if publishes_is_closed:
            # Only the kind that really has the flag may carry it.
            self.is_closed = False

    async def _closed(self) -> None:
        await asyncio.sleep(0.02)
        self.closed = True
        if hasattr(self, "is_closed"):
            self.is_closed = True


def _build_httpx_fake(settings=None) -> _PooledFake:
    client = _PooledFake("aclose", publishes_is_closed=True)
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None
    return client


def _build_cffi_fake(settings=None) -> _PooledFake:
    # curl_cffi's AsyncSession publishes no is_closed attribute, so neither may
    # this fake: see the class docstring.
    session = _PooledFake("close", publishes_is_closed=False)
    try:
        session._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        session._bound_loop = None
    return session


# Every loop-bound pooled client: (builder to patch, getter, module slot, fake).
SINGLETONS = [
    pytest.param(
        "_build_async_client",
        "get_async_http_client",
        "_DEFAULT_HTTP_CLIENT",
        _build_httpx_fake,
        id="httpx_client",
    ),
    pytest.param(
        "_build_cffi_session",
        "get_cffi_session",
        "_DEFAULT_CFFI_SESSION",
        _build_cffi_fake,
        id="cffi_session",
    ),
]


def _supersede_without_loop(monkeypatch, builder: str, getter: str, fake) -> tuple[object, object]:
    """Return ``(first, second)`` where the handover happened with no loop running.

    The first client is built on a real loop; the rebuild then happens from
    synchronous context, so ``loop is None`` and there is no running loop for a
    best-effort ``ensure_future`` to land on. Only a queued close can save it.
    """
    monkeypatch.setattr(http_mod, builder, lambda *args, **kwargs: fake())

    async def _on_a_live_loop():
        return getattr(http_mod, getter)()

    first = asyncio.run(_on_a_live_loop())
    assert first is not None
    # The loop that owned ``first`` is closed, and nothing is running now, so
    # the rebuild below happens with no loop to schedule a best-effort close on.
    second = getattr(http_mod, getter)()
    assert second is not None
    assert second is not first, "loop change must rebuild the pooled client"
    return first, second


@pytest.mark.parametrize("builder,getter,slot,fake", SINGLETONS)
def test_superseded_client_is_queued_for_close_when_no_loop_is_running(builder, getter, slot, fake, monkeypatch):
    """The missing half: the superseded client must be *tracked*, not dropped."""
    first, _second = _supersede_without_loop(monkeypatch, builder, getter, fake)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)
    assert first in queued, f"{getter} superseded a client without queueing it for close"


@pytest.mark.parametrize("builder,getter,slot,fake", SINGLETONS)
def test_queued_superseded_client_is_closed_by_the_next_drain(builder, getter, slot, fake, monkeypatch, real_sleeps):
    """Tracking pays off only if a live loop eventually closes the client."""
    first, _second = _supersede_without_loop(monkeypatch, builder, getter, fake)
    assert first.closed is False

    async def _fetch_like_drain():
        await http_mod._drain_pending_client_closes()

    asyncio.run(_fetch_like_drain())
    assert first.closed is True, f"{getter} left a superseded client unclosed after a drain"


@pytest.mark.parametrize("builder,getter,slot,fake", SINGLETONS)
def test_pending_close_queue_dedupes_and_stays_bounded(builder, getter, slot, fake, monkeypatch):
    """Re-superseding the same client must not double-queue it, and the backlog
    must stop growing at the cap (a stream of loop changes cannot leak memory).

    Driven through the real getter for each parametrized kind rather than by
    calling the queue helper directly: the property under test is that *this
    singleton's* supersession path dedupes and respects the cap, so a test that
    never called the getter would prove nothing about the cffi parameter it is
    nominally exercising. Each acquisition runs on its own freshly-created loop,
    so every handover is a genuine loop change and therefore a genuine
    supersede; the builder is a cycle, which lets the same client come round
    again and be superseded a second time while still queued.
    """
    cap = http_mod._PENDING_CLIENT_CLOSE_MAX
    built: list[_PooledFake] = []

    def _build(*args, **kwargs):
        instance = fake()
        built.append(instance)
        return instance

    monkeypatch.setattr(http_mod, builder, _build)

    def acquire_on_fresh_loop() -> _PooledFake:
        loop = asyncio.new_event_loop()
        try:

            async def _get():
                return getattr(http_mod, getter)()

            return loop.run_until_complete(_get())
        finally:
            loop.close()

    acquired = [acquire_on_fresh_loop() for _ in range(cap + 8)]

    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)

    # Every acquisition ran on its own loop, so each one superseded its
    # predecessor: cap+7 supersessions must have been squeezed down to the cap,
    # keeping the most recent ones.
    assert len(queued) <= cap, f"pending-close backlog grew past the cap: {len(queued)} > {cap}"
    assert acquired[-1] not in queued, "the current pooled client must not be queued for close"
    assert acquired[-2] in queued, f"{getter} dropped the client it just superseded"
    assert queued[-1] is acquired[-2], f"{getter} evicted the newest superseded client first"
    assert len(set(id(item) for item in queued)) == len(queued), (
        f"{getter} queued the same superseded client twice: dedupe is not holding"
    )
    for item in queued:
        assert queued.count(item) == 1, f"{getter} double-queued a superseded client"

    # Dedupe specifically: the queue helper is the thing that promises it, so
    # re-presenting an already-queued client through the same kind must not add
    # a second reference (a double-entry would let one client be closed twice
    # and would eat two cap slots).
    still_queued = queued[0]
    before = len(queued)
    assert http_mod._queue_superseded_close(still_queued) is None
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        after = list(http_mod._PENDING_CLIENT_CLOSE)
    assert len(after) == before, f"{getter} double-queued a client already awaiting close"
    assert after.count(still_queued) == 1


@pytest.mark.parametrize("builder,getter,slot,fake", SINGLETONS)
def test_superseded_client_is_also_closed_eagerly_on_a_live_loop(
    builder, getter, slot, fake, monkeypatch, real_sleeps
):
    """Inverse case: the queue must not have replaced the immediate close.

    When the handover happens on a loop that is still live -- the normal
    production case -- the superseded client should be gone before that request
    finishes, not only at the next drain. The cffi kind publishes no
    ``is_closed`` attribute, so a generic "already closed?" guard in the shared
    reaper would silently drop this eager close for sessions only.
    """
    monkeypatch.setattr(http_mod, builder, lambda *args, **kwargs: fake())

    async def _scenario():
        first = getattr(http_mod, getter)()
        foreign_loop = asyncio.new_event_loop()
        try:
            # Stand in for "this call arrived from a different loop".
            first._bound_loop = foreign_loop
            second = getattr(http_mod, getter)()
            await asyncio.sleep(0.1)  # let a scheduled close run on this loop
        finally:
            foreign_loop.close()
        return first, second

    first, second = asyncio.run(_scenario())
    assert second is not first
    assert getattr(http_mod, slot) is second, f"{getter} did not publish the rebuilt client in {slot}"
    assert first.closed is True, f"{getter} superseded a client without closing it on the live loop"


def test_both_singletons_queue_through_one_shared_implementation(monkeypatch, real_sleeps):
    """The anti-divergence guard: one reaper, two call sites.

    Both getters must reach the same queueing code, so a future pooled client
    cannot quietly re-implement (or omit) the discipline the way the cffi copy
    did relative to the httpx one.
    """
    seen: list[object] = []

    def _spy(client):
        seen.append(client)
        return None

    monkeypatch.setattr(http_mod, "_queue_superseded_close", _spy)
    _supersede_without_loop(monkeypatch, "_build_async_client", "get_async_http_client", _build_httpx_fake)
    _supersede_without_loop(monkeypatch, "_build_cffi_session", "get_cffi_session", _build_cffi_fake)
    assert len(seen) == 2, f"expected both singletons to queue through the shared helper, saw {len(seen)}"


def test_cffi_session_superseded_by_a_failing_rebuild_is_still_queued(monkeypatch):
    """Boundary: the loop changed and the rebuild itself raises ImportError.

    The session being replaced must not be dropped on the floor with no owner
    just because building its successor failed.
    """
    _first, second = _supersede_without_loop(
        monkeypatch, "_build_cffi_session", "get_cffi_session", _build_cffi_fake
    )

    def _boom(settings=None):
        raise ImportError("curl_cffi unavailable")

    monkeypatch.setattr(http_mod, "_build_cffi_session", _boom)

    async def _on_a_different_live_loop():
        return http_mod.get_cffi_session()

    assert asyncio.run(_on_a_different_live_loop()) is None, "a failed rebuild must yield no session"
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)
    assert second in queued, "a superseded session was dropped when the rebuild failed"


def test_cffi_fetcher_drains_pending_closes_like_the_http_fetcher(monkeypatch, real_sleeps):
    """Coverage: a queued close is only released if the cffi path drains too.

    The http fetcher drains on every request; the cffi fetcher never did, so
    sessions queued by a cffi loop change had no release point on that path.
    """
    drains = 0
    real_drain = http_mod._drain_pending_client_closes

    async def _counting_drain():
        nonlocal drains
        drains += 1
        await real_drain()

    monkeypatch.setattr(http_mod, "_drain_pending_client_closes", _counting_drain)
    monkeypatch.setattr(http_mod, "_build_cffi_session", lambda *a, **k: _build_cffi_fake())
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", None)

    async def _fake_http_fetcher(
        url,
        headers,
        timeout_seconds,
        max_content_length=0,
        *,
        policy,
    ):
        return "fell-back-to-httpx"

    monkeypatch.setattr(http_mod, "_default_http_fetcher", _fake_http_fetcher)

    async def _scenario():
        # A session whose transport call fails forces the documented fallback;
        # either way the fetch must have drained the queue first.
        return await http_mod._default_cffi_fetcher(
            "http://example.invalid/",
            {},
            1,
            1024,
            policy=HostPolicy.unrestricted(),
        )

    try:
        result = asyncio.run(_scenario())
        drains_from_fetch_path = drains
    finally:
        asyncio.run(http_mod.close_cffi_session())
    assert result == "fell-back-to-httpx"
    # Counted before the teardown above (whose shutdown drains as well), and the
    # httpx fetcher it fell back to is a stub that never drains -- so this one
    # drain can only have come from the cffi fetch path itself.
    assert drains_from_fetch_path == 1, (
        f"expected exactly the cffi path's own drain, got {drains_from_fetch_path}"
    )


SLOTS = ("_DEFAULT_HTTP_CLIENT", "_DEFAULT_CFFI_SESSION")


@pytest.fixture(autouse=True)
def _clean_pooled_state_release_checks(monkeypatch):
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


class _FakeClient:
    def __init__(self):
        self.is_closed = False
        self._bound_loop = None

    async def aclose(self):
        self.is_closed = True


def _fake_builder(settings):
    client = _FakeClient()
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None
    return client


async def _run():
    return http_mod.get_async_http_client()


def test_superseded_client_is_closed_on_loop_change(monkeypatch):
    monkeypatch.setattr(http_mod, "_build_async_client", _fake_builder)
    http_mod._DEFAULT_HTTP_CLIENT = None
    try:
        c1 = asyncio.run(_run())  # builds on loop A
        c2 = asyncio.run(_run())  # new loop B -> old client closed
        assert c1.is_closed is True, "superseded HTTP client was leaked"
        assert c2.is_closed is False
    finally:
        http_mod._DEFAULT_HTTP_CLIENT = None


def test_get_async_http_client_avoids_deprecated_get_event_loop(monkeypatch):
    # Prove the code path no longer depends on the deprecated get_event_loop:
    # make it raise and confirm the client is still obtainable (it must use
    # get_running_loop instead).
    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("get_event_loop is deprecated")),
    )
    # Force the construction path.
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None)

    async def inner():
        return get_async_http_client()

    client = asyncio.run(inner())
    assert client is not None
    assert not client.is_closed


def test_get_async_http_client_reuses_same_instance(monkeypatch):
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None)

    async def inner():
        return get_async_http_client(), get_async_http_client()

    a, b = asyncio.run(inner())
    assert a is b


def test_hot_path_client_reuse_skips_env_scan(monkeypatch):
    # Throughput (perf review 1.1): the per-fetch get_async_http_client() call
    # must not pay a full CrawlerSettings.from_mapping() env walk when the
    # pooled client is alive on the current loop. Settings are pool config
    # that is consumed only at construction, so a reuse call consults env zero
    # times while a construction call consults it exactly once.
    scans = {"count": 0}
    original = http_mod._default_settings

    def _counting_settings():
        scans["count"] += 1
        return original()

    monkeypatch.setattr(http_mod, "_default_settings", _counting_settings)
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None)

    async def inner():
        first = get_async_http_client()
        reuse_calls = scans["count"]
        second = get_async_http_client()
        third = get_async_http_client()
        return first, second is first, third is first, reuse_calls, scans["count"]

    first, reuses_second, reuses_third, reuse_calls, total = asyncio.run(inner())
    assert first is not None
    assert reuses_second and reuses_third
    # Construction walked the env exactly once; the two reuse calls added nothing.
    assert total == 1
    assert reuse_calls == 1


class _FakeStats:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes


def test_small_limit_hits_floor():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(256 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 100
        assert settings_mod._default_http_keepalive() == 50


def test_3gib_limit_scales_up():
    limit = 3 * 1024 * 1024 * 1024
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(limit)):
        # The pool ceiling must equal the admission's HTTP concurrency for the
        # same limit + pool, whatever the per-task RAM estimate is (zero drift).
        expected = max(
            100,
            settings_mod.max_http_concurrency_for(limit, settings_mod.derive_browser_pool_size(limit)),
        )
        assert settings_mod._default_http_max_connections() == expected
        assert settings_mod._default_http_keepalive() == max(20, min(128, expected // 2))
        # With the honest per-task RAM estimate the 3 GiB derivation sits at the
        # floor; the floor keeps the minimal-deployment pool usable (a bigger
        # box still scales above it -- see test_huge_limit_is_capped).
        assert expected >= 100


def test_huge_limit_is_capped():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(64 * 1024 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 512
        assert settings_mod._default_http_keepalive() == 128


def test_env_override_still_wins():
    # CrawlerSettings still honors an explicit env override over the derived default.
    with patch.dict("os.environ", {"BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS": "200"}):
        s = settings_mod.CrawlerSettings.from_mapping()
        assert s.http_max_connections == 200
