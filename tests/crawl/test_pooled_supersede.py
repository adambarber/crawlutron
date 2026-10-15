"""Superseded clients queue for close; drains stay bounded."""

import asyncio
from typing import Protocol

import pytest

import app.transport.http_client_pool as http_mod
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

    def stream(self, *args: object, **kwargs: object) -> "_BrokenStream":
        """Fail the fetch loop instantly with no network access."""
        return _BrokenStream()

    def isclose(self) -> bool:  # pragma: no cover - parity with the real kind
        return False


class _BrokenStream:
    """A stream whose body read raises, so the pump needs no origin."""

    status_code = 502
    headers = {}

    async def __aenter__(self) -> "_BrokenStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aiter_raw(self):
        raise OSError("no origin for a pooled-fake pump")


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
def test_superseded_client_survives_a_loop_changed_handoff_and_is_closed_by_the_next_acquire(
    seam, getter, fake, stub_builder
):
    """The missing half: a client superseded with no loop must be tracked, not dropped.

    Acquired on its own loop, the client is superseded by the next acquire on a
    fresh loop -- the context where nothing can schedule a close. The follow-on
    acquire is the live-loop pump every fetch path runs, and it is what must
    actually release the waiting client.
    """
    first, _second = _supersede_without_loop(stub_builder, seam, getter, fake)
    assert first.closed is False

    async def _release_through_teardown():
        await http_mod.close_async_http_client()

    asyncio.run(_release_through_teardown())
    assert first.closed is True, f"{getter} left a superseded client unreleased after the next fetch"


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_a_burst_of_loop_changes_releases_every_superseded_client(seam, getter, fake, stub_builder):
    """A stream of handovers cannot leak sockets.

    Each acquisition runs on its own freshly-created loop, so every handover is
    a genuine supersede with no live loop able to schedule the close. The next
    live-loop acquire is the pump every fetch path runs, and it must release
    every client the burst displaced.
    """
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

    acquired = [acquire_on_fresh_loop() for _ in range(8)]

    async def _drain_on_a_live_loop():
        await http_mod.close_async_http_client()

    asyncio.run(_drain_on_a_live_loop())
    assert all(a.closed for a in acquired[:-1]), (
        f"{getter} left a superseded client unreleased after a burst of loop changes"
    )


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
    assert http_first.closed is False and cffi_first.closed is False

    async def _drain_from_the_http_side():
        await http_mod.close_async_http_client()

    asyncio.run(_drain_from_the_http_side())
    # One shared queue: the httpx fetch's drain also releases the cffi session.
    assert http_first.closed is True and cffi_first.closed is True, (
        "both pooled clients must route their superseded close through one queue"
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
    assert second.closed is False, "the failing rebuild must not release the session mid-failure"

    async def _recover_through_teardown():
        await http_mod.close_async_http_client()

    asyncio.run(_recover_through_teardown())
    assert second.closed is True, "a superseded session was dropped when the rebuild failed"


def test_cffi_fetch_releases_a_queued_session_by_taking_one(stub_builder):
    """The cffi transport's acquire is also a release point.

    The http fetcher drains on every request; the cffi transport must too, or
    sessions queued by a cffi loop change have no release point on that path.
    A client is left sitting in the pending-close queue (the loop that owns it
    is gone), the cffi transport is asked for a session, and the queued client
    must come back closed -- a session that is tracked but never released is
    precisely the leak.
    """
    waiting, _second = _supersede_without_loop(stub_builder, "cffi", "get_cffi_session", _build_cffi_fake)
    assert waiting.closed is False

    async def _attempt():
        # The fake session cannot answer, so the attempt declines -- after the
        # transport's own drain has released the queued session.
        assert await _cffi_fetch(canonical("http://example.invalid/"), {}, 1, 1024) is None
        assert waiting.closed is True, (
            "the cffi transport took a session without releasing the one it "
            "superseded, so a superseded session has no release point on this path"
        )

    asyncio.run(_attempt())
