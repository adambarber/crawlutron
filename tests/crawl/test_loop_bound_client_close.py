"""P0-3: both loop-bound pooled clients must share the pending-close discipline.

``get_async_http_client`` and ``get_cffi_session`` are the same shape of object:
one pooled client per running event loop, rebuilt when the loop changes. The
rebuild has to *release* the superseded client, and the release is async while
the loop that owns it is already dying -- so the durable part of the pattern is
the bounded pending-close queue drained by the next fetch on a live loop, with
the immediate ``ensure_future`` only a best-effort shortcut.

The cffi session had only the shortcut: no queue, no drain, no dedupe, no cap,
so every loop change leaked one TLS session. These tests pin the queue
discipline for *both* singletons and pin that they run through one shared
implementation (the regression that let them diverge in the first place).
"""

import asyncio

import pytest

import app.crawl.strategies.http as http_mod


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
    """

    def __init__(self, closer: str) -> None:
        setattr(self, closer, self._closed)
        self.closed = False
        self.is_closed = False
        self._bound_loop = None

    async def _closed(self) -> None:
        await asyncio.sleep(0.02)
        self.closed = True
        self.is_closed = True


def _build_httpx_fake(settings=None) -> _PooledFake:
    client = _PooledFake("aclose")
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None
    return client


def _build_cffi_fake(settings=None) -> _PooledFake:
    session = _PooledFake("close")
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
    must stop growing at the cap (a stream of loop changes cannot leak memory)."""
    client = fake()
    cap = http_mod._PENDING_CLIENT_CLOSE_MAX
    for _ in range(3):
        http_mod._queue_superseded_close(client)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        assert list(http_mod._PENDING_CLIENT_CLOSE).count(client) == 1

    extras = [fake() for _ in range(cap + 5)]
    for extra in extras:
        http_mod._queue_superseded_close(extra)
    with http_mod._PENDING_CLIENT_CLOSE_LOCK:
        queued = list(http_mod._PENDING_CLIENT_CLOSE)
    assert len(queued) <= cap, f"pending-close backlog grew past the cap: {len(queued)} > {cap}"
    assert queued[-1] is extras[-1], "the newest superseded client must be retained"


@pytest.mark.parametrize("builder,getter,slot,fake", SINGLETONS)
def test_superseded_client_is_also_closed_eagerly_on_a_live_loop(builder, getter, slot, fake, monkeypatch, real_sleeps):
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

    async def _fake_http_fetcher(url, headers, timeout_seconds, max_content_length=0):
        return "fell-back-to-httpx"

    monkeypatch.setattr(http_mod, "_default_http_fetcher", _fake_http_fetcher)

    async def _scenario():
        # A session whose transport call fails forces the documented fallback;
        # either way the fetch must have drained the queue first.
        return await http_mod._default_cffi_fetcher("http://example.invalid/", {}, 1, 1024)

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
