"""HTTP client pool: shared per-loop client/session lifecycle, supersession, close queue and scaling."""

import asyncio
import contextlib
import inspect
import os
import warnings
from typing import Protocol
from unittest.mock import MagicMock, patch

import httpx
import pytest
from curl_cffi.requests import AsyncSession

import app.transport.http_client_pool as http_mod
from app.config import settings as settings_mod
from app.transport.fetchers import _cffi_fetch
from app.transport.http_client_pool import get_async_http_client
from tests._async_harnesses import run_loop_rebind_scenario
from tests._policy_factories import canonical
from tests._pool_harnesses import builder_for as _builder_for
from tests._timing_harnesses import await_until


class TestPooledClientBuilders:
    """The public seam that replaces monkeypatching the builders.

    The pooled clients are process-wide and created lazily on whichever loop
    asks first, so there is no per-test construction site to inject at. That
    absence is why the builders used to be patched on the module -- and why the
    module's own docstring advertised a late-bound lambda as a design goal, so
    the patch would be observed. The seam states the same capability as an
    argument and removes the reason for the indirection.
    """

    def test_a_supplied_builder_produces_the_pooled_client(self):
        sentinel = object()
        with http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            assert http_mod.get_cffi_session() is sentinel

    def test_the_builder_is_restored_after_the_seam_closes(self):
        sentinel = object()
        with http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            pass
        assert http_mod.get_cffi_session() is not sentinel

    def test_the_builder_is_restored_when_the_body_raises(self):
        """A failing test must not leave a fake in a process-wide singleton.

        This is why the seam restores rather than merely being callable: a
        builder left installed outlives the test that set it, and every later
        test in the process that asks for a pooled client then gets the
        previous test's double. That failure is order-dependent, which is the
        hardest kind to read.
        """
        sentinel = object()
        with contextlib.suppress(ValueError), http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            raise ValueError
        assert http_mod.get_cffi_session() is not sentinel

    def test_supplying_one_builder_leaves_the_other_alone(self):
        sentinel = object()
        with http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            assert not isinstance(http_mod.get_async_http_client(), _FakeClient)


class TestAsyncHttpClientLifecycle:
    async def test_http_async_client_close(self):
        from app.transport.http_client_pool import close_async_http_client

        # Build a client, then close it: the close path exercises a live one.
        get_async_http_client()
        await close_async_http_client()


class TestPendingClientCloseCap:
    def test_pending_client_close_has_cap(self):
        from app.transport import http_client_pool as http_mod

        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_MAX")
        assert http_mod._PENDING_CLIENT_CLOSE_MAX >= 1
        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_LOCK")

    def test_pending_client_close_cap_drops_oldest_silently(self):
        """When the cap is reached, the oldest entry is dropped (reference released)
        rather than scheduled for close via ``asyncio.ensure_future``. The drop path
        must work without a running event loop (sync tests / shutdown paths) so
        any attempt to schedule a future here would either raise or schedule on
        the wrong loop, silently leaking the sockets.
        """
        from unittest.mock import MagicMock

        from app.transport import http_client_pool as http_mod

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
            with contextlib.suppress(Exception):
                stale.close()
            http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
        assert len(http_mod._PENDING_CLIENT_CLOSE) == http_mod._PENDING_CLIENT_CLOSE_MAX

    def test_pending_client_close_dedupe_and_cap_are_atomic(self):
        src_path = "app/transport/http_client_pool.py"
        with open(src_path) as f:
            source = f.read()
        assert "already_queued = old_client in _PENDING_CLIENT_CLOSE" in source
        assert "if not already_queued:" in source


@pytest.fixture(autouse=True)
def _clean_pooled_state():
    """Start and end each test with the pooled state production never holds.

    ``reset_process_state`` is the whole of it, and it is the whole of it for a
    reason: it releases both pooled clients *and* clears the pending-close queue
    as one unit, because a queue still holding a client that was just released
    refers to a loop that can no longer drain it. Zeroing the module globals by
    hand was the same intent written out as four private assignments plus two
    reaches into the queue and its lock -- the two halves separately, which is
    precisely the half-state production cannot produce and no test should be
    asserting against.
    """
    http_mod.reset_process_state()
    yield
    http_mod.reset_process_state()
    assert not http_mod.has_process_state(), "the pooled state outlived the test that used it"


class _TrackedClient(Protocol):
    """What a supersede test reads off a client it did not build itself.

    The getter is named at runtime, so its return cannot be named statically;
    declaring it as ``object`` made every read of ``closed`` an attribute error
    rather than a question about the double. Both the ``_PooledFake`` fakes and
    the real-shaped doubles below carry exactly these two, and they are the two
    the loop-identity and reaper behaviour turn on.
    """

    closed: bool
    _bound_loop: asyncio.AbstractEventLoop | None


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
    every real session as already closed. Each descriptor's own ``is_closed``
    predicate is what decides reuse-safety.
    """

    def __init__(self, closer: str, *, publishes_is_closed: bool) -> None:
        setattr(self, closer, self._closed)
        self.closed = False
        # Annotated rather than left to the first assignment: without it the
        # attribute's type is ``None``, and every site that binds a live loop
        # afterwards -- which is what the pool's whole loop-identity question is
        # about -- was an error.
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        if publishes_is_closed:
            # Only the kind that really has the flag may carry it.
            self.is_closed = False

    async def _closed(self) -> None:
        await asyncio.sleep(0.02)
        self.closed = True
        if hasattr(self, "is_closed"):
            self.is_closed = True


def _bind_to_running_loop(client) -> None:
    """Bind a fake client to the running loop, or leave it unbound if there is none.

    A fake built outside a loop is how the pool's "was this closed from the loop
    it was made on" question gets answered honestly, and the try/except was
    written out at every construction site.
    """
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None


def _expected_connections(limit: int) -> int:
    """The pool ceiling the settings must derive for a memory limit.

    Spelled out per test, the expectation and the derivation could be changed
    independently, which is the one thing a ceiling test must not allow.
    """
    return max(
        100,
        settings_mod.max_http_concurrency_for(limit, settings_mod.derive_browser_pool_size(limit)),
    )


def _build_httpx_fake(settings=None) -> _PooledFake:
    client = _PooledFake("aclose", publishes_is_closed=True)
    _bind_to_running_loop(client)
    return client


def _build_cffi_fake(settings=None) -> _PooledFake:
    # Curl_cffi's AsyncSession publishes no is_closed attribute, so neither may
    # this fake: see the class docstring.
    session = _PooledFake("close", publishes_is_closed=False)
    try:
        session._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        session._bound_loop = None
    return session


# Every loop-bound pooled client: (the seam keyword that supplies its builder,
# its public getter, the fake that builder should hand out).
SINGLETONS = [
    pytest.param("http", "get_async_http_client", _build_httpx_fake, id="httpx_client"),
    pytest.param("cffi", "get_cffi_session", _build_cffi_fake, id="cffi_session"),
]


def _supersede_without_loop(
    stub_builder, seam: str, getter: str, fake
) -> tuple[_TrackedClient, _TrackedClient]:
    """Return ``(first, second)`` where the handover happened with no loop running.

    The first client is built on a real loop; the rebuild then happens from
    synchronous context, so ``loop is None`` and there is no running loop for a
    best-effort ``ensure_future`` to land on. Only a queued close can save it.
    """
    stub_builder(seam, _builder_for(fake))

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


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_superseded_client_is_queued_for_close_when_no_loop_is_running(seam, getter, fake, stub_builder):
    """The missing half: the superseded client must be *tracked*, not dropped."""
    first, _second = _supersede_without_loop(stub_builder, seam, getter, fake)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)
    assert first in queued, f"{getter} superseded a client without queueing it for close"


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_queued_superseded_client_is_closed_by_the_next_drain(seam, getter, fake, stub_builder):
    """Tracking pays off only if a live loop eventually closes the client."""
    first, _second = _supersede_without_loop(stub_builder, seam, getter, fake)
    assert first.closed is False

    async def _fetch_like_drain():
        await http_mod._drain_pending_client_closes()

    asyncio.run(_fetch_like_drain())
    assert first.closed is True, f"{getter} left a superseded client unclosed after a drain"


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_pending_close_queue_dedupes_and_stays_bounded(seam, getter, fake, stub_builder):
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

    stub_builder(seam, _build)

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


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_superseded_client_is_also_closed_eagerly_on_a_live_loop(seam, getter, fake, stub_builder):
    """Inverse case: the queue must not have replaced the immediate close.

    When the handover happens on a loop that is still live -- the normal
    production case -- the superseded client should be gone before that request
    finishes, not only at the next drain. The cffi kind publishes no
    ``is_closed`` attribute, so a generic "already closed?" guard in the shared
    reaper would silently drop this eager close for sessions only.

    There is no assertion that the rebuilt client landed in the pool's private
    module global. That used to be read directly, and it is a caching detail
    rather than a property: the fixture's teardown asserts the module holds no
    pooled client after the test, which cannot be true unless the rebuild
    published one, and it does so through the module's public surface.
    """
    stub_builder(seam, _builder_for(fake))

    first, second = asyncio.run(run_loop_rebind_scenario(lambda: getattr(http_mod, getter)()))
    assert second is not first
    assert first.closed is True, f"{getter} superseded a client without closing it on the live loop"


def test_both_singletons_queue_through_one_shared_implementation(stub_builder):
    """The anti-divergence guard: one reaper, two call sites.

    Both getters must land their superseded client in the same queue, so a future
    pooled client cannot quietly re-implement (or omit) the discipline the way the
    cffi copy did relative to the httpx one. The queue is read rather than the
    queueing helper spied: how many times a private helper was *called* is not
    the property, and where the references end up is. A count would also have
    passed against a helper that queued nothing at all.
    """
    http_first, _ = _supersede_without_loop(stub_builder, "http", "get_async_http_client", _build_httpx_fake)
    cffi_first, _ = _supersede_without_loop(stub_builder, "cffi", "get_cffi_session", _build_cffi_fake)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)
    assert http_first in queued and cffi_first in queued, (
        f"both pooled clients must supersede into the one shared queue; queued {len(queued)}: {queued}"
    )


def test_cffi_session_superseded_by_a_failing_rebuild_is_still_queued(stub_builder):
    """Boundary: the loop changed and the rebuild itself fails.

    The session being replaced must not be dropped on the floor with no owner
    just because building its successor failed. The failure itself propagates:
    both pooled clients are declared dependencies, so there is no missing-package
    state to degrade into -- a silent ``None`` here was read by the cffi fetcher
    as "impersonation unavailable" and downgraded a TLS-fingerprinted fetch to
    plain httpx while reporting success.
    """
    _first, second = _supersede_without_loop(stub_builder, "cffi", "get_cffi_session", _build_cffi_fake)

    def _boom(settings=None):
        raise ImportError("curl_cffi unavailable")

    stub_builder("cffi", _boom)

    async def _on_a_different_live_loop():
        return http_mod.get_cffi_session()

    with pytest.raises(ImportError, match="curl_cffi unavailable"):
        asyncio.run(_on_a_different_live_loop())
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)
    assert second in queued, "a superseded session was dropped when the rebuild failed"


def test_cffi_fetch_drains_pending_closes_before_taking_the_session(stub_builder):
    """Coverage: a queued close is only released if the cffi path drains too.

    The http fetcher drains on every request; the cffi transport never did, so
    sessions queued by a cffi loop change had no release point on that path.

    Proved by outcome rather than by counting drains. This used to spy on the
    private drain helper and assert it was called exactly once, which proved the
    call and not the release: a drain that emptied the queue without closing
    anything scored the same. A client is now left sitting in the queue, the
    cffi transport is asked for a session, and the assertion is that the queued
    client came back closed -- the property a caller can actually observe, since
    a session that is tracked but never released is precisely the leak.

    ``_cffi_fetch`` is driven directly because it is the function that owns the
    drain. This test used to go through the impersonating fetcher and stub the
    plain-httpx fallback, which cost a fake that could not be reached (the
    fetcher is a ``functools.partial`` built at import, so patching the module
    attribute after the fact does nothing) and made the assertion ambiguous
    anyway: the total it counted included the httpx path's own drain, so it
    could not tell one drain from two. The transport returns ``None`` for
    "could not be made", so the fetch below is the cffi path's own.
    """
    stub_builder("cffi", _builder_for(_build_cffi_fake))
    waiting = _PooledFake("close", publishes_is_closed=False)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        http_mod._PENDING_CLIENT_CLOSE.append(waiting)

    async def _attempt():
        return await _cffi_fetch(canonical("http://example.invalid/"), {}, 1, 1024)

    try:
        assert asyncio.run(_attempt()) is None, "the fake session cannot answer, so the attempt declines"
        assert waiting.closed is True, (
            "the cffi transport took a session without releasing the client it had queued for close, "
            "so a superseded session has no release point on this path"
        )
    finally:
        asyncio.run(http_mod.close_cffi_session())


class _PooledClientDouble:
    """The half of a pooled-client double that does not depend on the library.

    ``closed`` is a class attribute so a subclass only has to say what is
    different about its shape; assigning it in ``_finish_close`` shadows it per
    instance, which is what the assertions read. ``_bound_loop`` is here because
    ``_LoopBoundPooledClient`` writes it on every build. The ``sleep(0)`` is the
    yield that lets a scheduled close finish before a test looks at the flag.
    """

    closed = False

    def __init__(self) -> None:
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    async def _finish_close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


class _RealShapedSession(_PooledClientDouble):
    """Curl_cffi-shaped: a coroutine ``close`` and NO ``is_closed`` attribute."""

    async def close(self) -> None:
        await self._finish_close()


class _RealShapedHttpClient(_PooledClientDouble):
    """Httpx-shaped: a coroutine ``aclose`` and no sync ``close``."""

    def __init__(self) -> None:
        super().__init__()
        self.is_closed = False

    async def aclose(self) -> None:
        await self._finish_close()
        self.is_closed = True


class _ThirdKind(_PooledClientDouble):
    """A pooled kind that is neither httpx nor curl_cffi shaped, and is meant to be.

    It reaps under ``aclose`` like httpx but publishes no ``is_closed``, which
    is the combination neither library actually has.
    """

    async def aclose(self) -> None:
        await self._finish_close()


def test_real_pooled_classes_have_no_synchronous_close():
    """The premise of, pinned against the installed libraries.

    If either class ever gains a synchronous close, the reasoning behind
    ``_release_dropped_client`` has to be revisited -- this test is the trip
    wire that says so instead of a docstring that quietly stays wrong.
    """
    assert not hasattr(httpx.AsyncClient(), "close"), "httpx gained a sync close"
    assert inspect.iscoroutinefunction(AsyncSession.close), "cffi close is no longer async"
    assert not hasattr(AsyncSession, "is_closed"), "cffi now publishes is_closed"


def _released_on_a_live_loop(client, what: str) -> bool:
    """``_release_dropped_client`` on `client` must reach a real close, not a reference drop.

    The wait is on the close itself: the release schedules the teardown, and this
    returns as soon as the scheduled coroutine has finished. A fixed sleep could
    only ever sample the flag afterwards, which is a bet about how quickly the
    loop got round to running the task -- and the httpx twin of this test and the
    cffi one differed only in that sleep's text, so one of them could pass while
    the drop was broken for its kind.

    ``what`` names the client in the failure message; "the teardown never ran"
    in a 1200-test run does not say which kind failed.
    """

    async def _scenario():
        http_mod._release_dropped_client(client)
        await await_until(lambda: client.closed, f"{what}'s teardown never ran on a live loop")

    asyncio.run(_scenario())
    return client.closed is True


def test_displaced_client_is_actually_released_when_a_loop_is_running():
    """The cap's displacement must reach a real close, not a reference drop.

    Both kinds in one test. The cffi session's coroutine ``close`` is the only
    teardown it has, so if the release dropped the reference for that kind
    instead of closing it, only a test driving *that* kind would notice -- and
    the pair differed only in a sleep's text.
    """
    assert _released_on_a_live_loop(_RealShapedHttpClient(), "the displaced client")
    assert _released_on_a_live_loop(_RealShapedSession(), "the displaced AsyncSession")


def test_displaced_client_with_no_running_loop_is_not_left_as_a_broken_coroutine():
    """Boundary: sync context must not create a never-awaited coroutine.

    The queue is the only owner in that case, so the reference is dropped --
    but doing it by building a coroutine nobody will await would trade a socket
    for a RuntimeWarning on every capped rebuild. The assertion is the warning
    escalation plus the double's own ``closed`` flag, not a spy on the private
    ``_safe_aclose`` helper this used to replace: an un-awaited coroutine raises
    "coroutine ... was never awaited" when it is collected, and warnings are
    escalated to errors, so nothing can create one without the test failing.
    Spying named the route rather than the property, and would have passed
    against a rebuild that scheduled a close by some other path.
    """
    displaced = _RealShapedHttpClient()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        http_mod._release_dropped_client(displaced)

    assert displaced.closed is False, "a teardown was scheduled with no loop able to run it"


def test_none_displacement_is_a_noop():
    """Inverse: an undisplaced enqueue must not blow up the release path."""
    http_mod._release_dropped_client(None)


def test_real_shaped_session_is_closed_eagerly_on_a_live_loop(stub_builder):
    """The eager close must fire for a session with no ``is_closed``.

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

    stub_builder("cffi", _build)

    first, second = asyncio.run(run_loop_rebind_scenario(http_mod.get_cffi_session))
    assert second is not first
    assert first.closed is True, (
        "get_cffi_session superseded a real-shaped AsyncSession without closing "
        "it on the live loop -- the eager close never fires because the reaper "
        'reads a missing is_closed attribute as "already closed"'
    )


def install_httpx_double(stub_builder) -> list[_RealShapedHttpClient]:
    """Hand out real-shaped httpx doubles through the public seam; return them.

    The list is the point: the getter is typed as handing back a real
    ``httpx.AsyncClient``, whose ``is_closed`` is a read-only property and which
    has no ``closed`` at all, so a test that wants to read or set either has to
    name the double the builder actually built. Two tests were each spelling this
    builder out, and the second copy is the one that would have drifted.
    """
    built: list[_RealShapedHttpClient] = []

    def _build(*args, **kwargs):
        client = _RealShapedHttpClient()
        client._bound_loop = asyncio.get_running_loop()
        built.append(client)
        return client

    stub_builder("http", _build)
    return built


def test_httpx_kind_keeps_its_eager_close_after_the_gate_moves(stub_builder):
    """Inverse: moving the gate to the descriptor predicate must not cost the
    pooled httpx client the eager close it already had.
    """
    # The double list is not read here: the scenario hands the same client back
    # through the getter, and ``first`` is that client. Installed for the
    # builder, not for the list.
    install_httpx_double(stub_builder)

    first, second = asyncio.run(run_loop_rebind_scenario(http_mod.get_async_http_client))
    assert second is not first
    assert first.closed is True, "the pooled httpx client lost its eager close"


def test_already_closed_pooled_client_is_not_re_closed_on_the_live_loop(stub_builder):
    """Boundary: the descriptor predicate still recognises a closed client.

    A client the caller reports as closed owns no live sockets, so the rebuild
    must drop it quietly instead of scheduling a pointless teardown task.

    The assertion is the double's own ``closed`` flag, not a spy on the private
    close helper this used to replace. ``closed`` is only ever set by the close
    coroutine itself, and one loop yield is enough for a teardown that WAS
    scheduled to start and finish -- the task goes on the ready queue by
    ``ensure_future`` and the close yields once itself. So the flag staying
    False is positive evidence that nothing was scheduled, and it is evidence
    about the client rather than about one chosen route to closing it.
    """
    # The double the builder built, kept so the scenario reads the same object
    # production will: the getter is typed as handing back a real
    # ``httpx.AsyncClient``, whose ``is_closed`` is a read-only property and
    # which has no ``closed`` at all -- the two assignments below are on a
    # double, and naming it is what says so.
    built = install_httpx_double(stub_builder)

    async def _scenario():
        first = http_mod.get_async_http_client()
        assert built, "the builder must have produced a client"
        tracked = built[0]
        tracked.is_closed = True  # someone else already closed it
        tracked.closed = False  # would flip only if a teardown were scheduled
        second = http_mod.get_async_http_client()
        # One yield is enough for a scheduled teardown to run: the task is
        # put on the ready queue by ensure_future, and this hands the loop
        # back exactly once, so the coroutine starts and (itself yielding
        # once) finishes.
        await asyncio.sleep(0)
        return first, second

    first, second = asyncio.run(_scenario())

    assert second is not first, "a closed pooled client must be rebuilt"
    assert built, "the builder must have produced a client"
    assert built[0].closed is False, "an already-closed client was scheduled for close"


def test_a_third_pooled_kind_is_not_left_out_by_the_httpx_attribute_shape():
    """'s generalization: the closed-decision belongs to the descriptor.

    The merged guard probed for an attribute name (``is_closed``) instead of
    asking the descriptor that owns the kind. Any future pooled client that
    does not publish that attribute inherits the cffi bug exactly: treated as
    already closed, never closed eagerly. This third kind is deliberately
    neither httpx nor curl_cffi shaped.
    """
    import threading

    # The descriptor keeps its pooled instance in the module global named by
    # ``slot``, so a slot has to name a global that exists. This borrows the
    # curl_cffi one rather than inventing a third: nothing in this test asks
    # for the cffi pool, and the module's fixture releases the slot at teardown.
    # Adding a name to the module for the convenience of a test is how the slot
    # list and its test-support grew together in the first place.
    third = http_mod._LoopBoundPooledClient(
        slot="_DEFAULT_CFFI_SESSION",
        build=lambda settings: _ThirdKind(),
        is_closed=lambda client: False,
        lock=threading.Lock(),
        consults_settings=False,
    )

    first, second = asyncio.run(run_loop_rebind_scenario(third.get))
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
    _bind_to_running_loop(client)
    return client


async def _run():
    return http_mod.get_async_http_client()


def test_superseded_client_is_closed_on_loop_change(stub_builder):
    stub_builder("http", _fake_builder)
    c1 = asyncio.run(_run())  # builds on loop A
    c2 = asyncio.run(_run())  # new loop B -> old client closed
    assert c1.is_closed is True, "superseded HTTP client was leaked"
    assert c2.is_closed is False


def test_get_async_http_client_avoids_deprecated_get_event_loop(monkeypatch):
    # Prove the code path no longer depends on the deprecated get_event_loop:
    # make it raise and confirm the client is still obtainable (it must use
    # get_running_loop instead).
    # The construction path is forced by the fixture, which released the pooled
    # client before this body ran; naming the module global to null it again is
    # what this used to do, and it made the test depend on a name the module
    # keeps for its own bookkeeping.
    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("get_event_loop is deprecated")),
    )

    async def inner():
        return get_async_http_client()

    client = asyncio.run(inner())
    assert client is not None
    assert not client.is_closed


def test_get_async_http_client_reuses_same_instance():
    async def inner():
        return get_async_http_client(), get_async_http_client()

    a, b = asyncio.run(inner())
    assert a is b


def test_hot_path_client_reuse_skips_env_scan():
    # Throughput (perf review 1.1): the per-fetch get_async_http_client() call
    # must not pay a full CrawlerSettings.from_mapping() env walk when the
    # pooled client is alive on the current loop. Settings are pool config
    # that is consumed only at construction, so a reuse call consults env zero
    # times while a construction call consults it exactly once.
    #
    # The env walk is counted by watching ``CrawlerSettings.from_mapping`` rather
    # than the module's own ``_default_settings``, which is a private name this
    # test had to reach into. Counting at the derivation is the stronger claim
    # anyway: it would catch the walk being paid by any route, where counting one
    # private helper would not.
    scans = {"count": 0}
    original = settings_mod.CrawlerSettings.from_mapping

    def _counting_from_mapping(*args, **kwargs):
        scans["count"] += 1
        return original(*args, **kwargs)

    settings_mod.CrawlerSettings.from_mapping = staticmethod(_counting_from_mapping)
    try:

        async def inner():
            first = get_async_http_client()
            reuse_calls = scans["count"]
            second = get_async_http_client()
            third = get_async_http_client()
            return first, second is first, third is first, reuse_calls, scans["count"]

        first, reuses_second, reuses_third, reuse_calls, total = asyncio.run(inner())
    finally:
        settings_mod.CrawlerSettings.from_mapping = original
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
        expected = _expected_connections(limit)
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
    with patch.dict("os.environ", {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "200"}):
        s = settings_mod.CrawlerSettings.from_mapping()
        assert s.http_max_connections == 200


def test_the_pool_default_is_derived_per_construction_not_frozen_at_import():
    """The derivation READS the memory limit, so it is I/O -- and a call in the
    class body made it import-time I/O, run once and frozen for the process.

    That is not a style point. A box whose limit only becomes readable after
    the module was first imported (a cgroup limit the runtime applies late), or
    whose read failed and silently took the 4 GiB fallback, kept that wrong
    ceiling for its whole life with nothing in the config saying so. Deriving
    per construction means the number is always derived from the limit the
    running process actually has.
    """
    limit = 8 * 1024 * 1024 * 1024
    expected = _expected_connections(limit)
    with (
        patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(limit)),
        patch.dict("os.environ"),
    ):
        os.environ.pop("CRAWLUTRON_HTTP_MAX_CONNECTIONS", None)
        os.environ.pop("CRAWLUTRON_HTTP_MAX_KEEPALIVE", None)
        derived = settings_mod.CrawlerSettings()
        assert derived.http_max_connections == expected
        assert derived.http_max_keepalive == max(20, min(128, expected // 2))
        # The env path must derive too. A ``default_factory`` field has no class
        # attribute to read, so this is the line where that trap shows up.
        from_env = settings_mod.CrawlerSettings.from_mapping()
        assert from_env.http_max_connections == expected
        assert from_env.http_max_keepalive == max(20, min(128, expected // 2))
