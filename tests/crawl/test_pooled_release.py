"""Displaced clients release eagerly on a live loop, never twice."""

import asyncio
import inspect
import warnings

import httpx
from curl_cffi.requests import AsyncSession

import app.transport.http_client_pool as http_mod
from app.config import settings as settings_mod
from app.transport.http_client_pool import get_async_http_client
from tests._async_harnesses import run_loop_rebind_scenario
from tests._timing_harnesses import await_until
from tests.crawl.test_http_client_pool import _FakeClient
from tests.crawl.test_pooled_supersede import _bind_to_running_loop


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
    # The double list is not read here: the scenario hands the same client back through the getter, and
    # ``first`` is that client. Installed for the builder, not for the list.
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
    built = install_httpx_double(stub_builder)

    async def _scenario():
        first = http_mod.get_async_http_client()
        assert built, "the builder must have produced a client"
        tracked = built[0]
        tracked.is_closed = True  # someone else already closed it
        tracked.closed = False  # would flip only if a teardown were scheduled
        second = http_mod.get_async_http_client()
        # One yield is enough for a scheduled teardown to run: the task is put on the ready queue by
        # ensure_future, and this hands the loop back exactly once, so the coroutine starts and (itself
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

    # The descriptor keeps its pooled instance in the module global named by ``slot``, so a slot has to name a
    # global that exists.
    third = http_mod._LoopBoundPooledClient(
        slot="_DEFAULT_CFFI_SESSION",
        build=lambda settings: _ThirdKind(),
        is_closed=lambda client: False,
        lock=threading.Lock(),
        consults_settings=False,
        namespace=vars(http_mod),
    )

    first, second = asyncio.run(run_loop_rebind_scenario(third.get))
    assert second is not first
    assert first.closed is True, (
        "a pooled kind without an is_closed attribute was treated as already "
        "closed and never released eagerly"
    )


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
    # Throughput (perf review 1.1): the per-fetch get_async_http_client() call must not pay a full
    # CrawlerSettings.from_mapping() env walk when the pooled client is alive on the current loop.
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
