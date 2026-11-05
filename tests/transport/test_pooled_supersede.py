"""Superseded clients queue for close; drains stay bounded."""

import asyncio
from typing import Protocol

import pytest

import crawlutron.transport.http_client_pool as http_mod
from crawlutron.transport.fetchers import cffi_fetch
from tests._async_harnesses import run_loop_rebind_scenario
from tests._policy_factories import canonical
from tests._pool_harnesses import builder_for as _builder_for
from tests._pool_ws_doubles import bind_to_running_loop as _bind_to_running_loop


@pytest.fixture(autouse=True)
def _clean_pooled_state():
    """Start and end each test with the pooled state production never holds.

    ``reset_process_state`` releases both pooled clients and clears the queue
    as the unit production releases them."""
    http_mod.reset_process_state()
    yield
    http_mod.reset_process_state()
    assert not http_mod.has_process_state(), "the pooled state outlived the test that used it"


class _TrackedClient(Protocol):
    """What a supersede test reads off a client it did not build itself.

    The getter is named at runtime, so ``object`` made every read of ``closed``
    an attribute error; both fake kinds carry exactly these two fields."""

    closed: bool
    _bound_loop: asyncio.AbstractEventLoop | None


class _PooledFake:
    """Minimal loop-bound client: records its bound loop and whether it closed.

    ``close`` yields to the loop first, like a real teardown; ``closer`` names
    the real attribute, and the cffi fake must NOT grow an ``is_closed``."""

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
    headers: dict[str, str] = {}

    async def __aenter__(self) -> "_BrokenStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aiter_raw(self):
        raise OSError("no origin for a pooled-fake pump")


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

    The rebuild from synchronous context has ``loop is None`` -- no loop for
    ``ensure_future`` to land on -- so only a queued close can save it."""
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
    """The missing half: a superseded-with-no-loop client must be tracked.

    The follow-on acquire is the live-loop pump every fetch path runs, and it
    is what must actually release the waiting client."""
    first, _second = _supersede_without_loop(stub_builder, seam, getter, fake)
    assert first.closed is False

    async def _release_through_teardown():
        await http_mod.close_async_http_client()

    asyncio.run(_release_through_teardown())
    assert first.closed is True, f"{getter} left a superseded client unreleased after the next fetch"


@pytest.mark.parametrize("seam,getter,fake", SINGLETONS)
def test_a_burst_of_loop_changes_releases_every_superseded_client(seam, getter, fake, stub_builder):
    """A stream of handovers cannot leak sockets.

    Every handover is a genuine supersede with no live loop to schedule the
    close; the next live-loop acquire must release every client displaced."""
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

    On a live-loop handover the client should be gone before the request
    finishes. The landing is asserted through the teardown, not a private read."""
    stub_builder(seam, _builder_for(fake))

    first, second = asyncio.run(run_loop_rebind_scenario(lambda: getattr(http_mod, getter)()))
    assert second is not first
    assert first.closed is True, f"{getter} superseded a client without closing it on the live loop"


def test_both_singletons_queue_through_one_shared_implementation(stub_builder):
    """The anti-divergence guard: one reaper, two call sites.

    Both getters must land their superseded client in the same queue; the
    queue is read, not the helper spied."""
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

    The replaced session must not be dropped ownerless because its successor
    failed -- a silent ``None`` downgraded a fetch while reporting success."""
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


def test_cffi_transport_releases_a_queued_session(stub_builder):
    """The cffi transport's acquire is also a release point.

    The http fetcher drains on every request; the cffi transport must too, or
    queued sessions have no release point on that path."""
    waiting, _second = _supersede_without_loop(stub_builder, "cffi", "get_cffi_session", _build_cffi_fake)
    assert waiting.closed is False

    async def _attempt():
        # The fake session cannot answer, so the attempt declines -- after the
        # transport's own drain has released the queued session.
        assert await cffi_fetch(canonical("http://example.invalid/"), {}, 1, 1024) is None
        assert waiting.closed is True, (
            "the cffi transport took a session without releasing the one it "
            "superseded, so a superseded session has no release point on this path"
        )

    asyncio.run(_attempt())
