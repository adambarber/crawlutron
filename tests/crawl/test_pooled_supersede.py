"""Superseded clients queue for close; drains stay bounded."""

import asyncio
from typing import Protocol

import pytest

import app.transport.http_client_pool as http_mod
import app.transport.pooled_clients as pool_mod
from app.transport.fetchers import _cffi_fetch
from tests._async_harnesses import run_loop_rebind_scenario
from tests._policy_factories import canonical
from tests._pool_harnesses import builder_for as _builder_for


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


def _build_httpx_fake(settings=None) -> _PooledFake:
    client = _PooledFake("aclose", publishes_is_closed=True)
    _bind_to_running_loop(client)
    return client


def _build_cffi_fake(settings=None) -> _PooledFake:
    # Curl_cffi's AsyncSession publishes no is_closed attribute, so neither may this fake: see the class
    # docstring.
    session = _PooledFake("close", publishes_is_closed=False)
    try:
        session._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        session._bound_loop = None
    return session


# Every loop-bound pooled client: (the seam keyword that supplies its builder, its public getter, the fake
# that builder should hand out).
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
    # The loop that owned ``first`` is closed, and nothing is running now, so the rebuild below happens with
    # no loop to schedule a best-effort close on.
    second = getattr(http_mod, getter)()
    assert second is not None
    assert second is not first, "loop change must rebuild the pooled client"
    return first, second


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_superseded_client_is_queued_for_close_when_no_loop_is_running(seam, getter, fake, stub_builder):
    """The missing half: the superseded client must be *tracked*, not dropped."""
    first, _second = _supersede_without_loop(stub_builder, seam, getter, fake)
    with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(pool_mod._PENDING_CLIENT_CLOSE)
    assert first in queued, f"{getter} superseded a client without queueing it for close"


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_queued_superseded_client_is_closed_by_the_next_drain(seam, getter, fake, stub_builder):
    """Tracking pays off only if a live loop eventually closes the client."""
    first, _second = _supersede_without_loop(stub_builder, seam, getter, fake)
    assert first.closed is False

    async def _fetch_like_drain():
        await pool_mod._drain_pending_client_closes()

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
    cap = pool_mod._PENDING_CLIENT_CLOSE_MAX
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

    with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(pool_mod._PENDING_CLIENT_CLOSE)

    # Every acquisition ran on its own loop, so each one superseded its predecessor: cap+7 supersessions must
    # have been squeezed down to the cap, keeping the most recent ones.
    assert len(queued) <= cap, f"pending-close backlog grew past the cap: {len(queued)} > {cap}"
    assert acquired[-1] not in queued, "the current pooled client must not be queued for close"
    assert acquired[-2] in queued, f"{getter} dropped the client it just superseded"
    assert queued[-1] is acquired[-2], f"{getter} evicted the newest superseded client first"
    assert len(set(id(item) for item in queued)) == len(queued), (
        f"{getter} queued the same superseded client twice: dedupe is not holding"
    )
    for item in queued:
        assert queued.count(item) == 1, f"{getter} double-queued a superseded client"

    # Dedupe specifically: the queue helper is the thing that promises it, so re-presenting an already-queued
    still_queued = queued[0]
    before = len(queued)
    assert pool_mod._queue_superseded_close(still_queued) is None
    with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
        after = list(pool_mod._PENDING_CLIENT_CLOSE)
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
    with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(pool_mod._PENDING_CLIENT_CLOSE)
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
    with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(pool_mod._PENDING_CLIENT_CLOSE)
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
    with pool_mod._PENDING_CLIENT_CLOSE_LOCK:
        pool_mod._PENDING_CLIENT_CLOSE.append(waiting)

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
