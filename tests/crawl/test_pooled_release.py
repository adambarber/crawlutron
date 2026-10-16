"""Displaced clients release eagerly on a live loop, never twice."""

import asyncio
import inspect

import httpx
from curl_cffi.requests import AsyncSession

import app.transport.http_client_pool as http_mod
from app.config import settings as settings_mod
from app.transport.http_client_pool import get_async_http_client
from tests._async_harnesses import run_loop_rebind_scenario
from tests.crawl.test_http_client_pool import _FakeClient
from tests.crawl.test_pooled_supersede import _bind_to_running_loop


class _PooledClientDouble:
    """The half of a pooled-client double independent of the library.

    ``closed`` is a class attribute so a subclass states only what differs;
    ``_bound_loop`` is set on every build, and ``sleep(0)`` lets a close finish.
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

    It reaps under ``aclose`` like httpx but publishes no ``is_closed``, the
    one combination neither real library has.
    """

    async def aclose(self) -> None:
        await self._finish_close()


def test_real_pooled_classes_have_no_synchronous_close():
    """The premise of, pinned against the installed libraries.

    If either class ever gains a sync close, the reasoning behind
    ``_release_dropped_client`` must be revisited -- this is the trip wire.
    """
    assert not hasattr(httpx.AsyncClient(), "close"), "httpx gained a sync close"
    assert inspect.iscoroutinefunction(AsyncSession.close), "cffi close is no longer async"
    assert not hasattr(AsyncSession, "is_closed"), "cffi now publishes is_closed"


def test_real_shaped_session_is_closed_eagerly_on_a_live_loop(stub_builder):
    """The eager close must fire for a session with no ``is_closed``.

    Driven through the production rebuild path with a curl_cffi-shaped object,
    so the superseded session is closed when the triggering request finishes.
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

    The list is the point: the getter is typed to hand back a real
    ``httpx.AsyncClient``, so a test must name the double the builder built.
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

    A client the caller reports closed owns no live sockets, so the rebuild
    must drop it quietly; the ``closed`` flag staying False is the evidence.
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
