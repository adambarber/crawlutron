"""Pooled client lifecycle shared by the HTTP facade: build once per loop, supersede cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.cookiejar import CookieJar as _StdlibCookieJar
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

import httpx

from app.transport.timeouts import Timeouts
from app.vocabulary.identity import DEFAULT_USER_AGENT

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings

_DEFAULT_HTTP_CLIENT: httpx.AsyncClient | None = None


_HTTP_CLIENT_INIT_LOCK = threading.Lock()


def _build_async_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with configurable connection pool."""
    if settings is None:
        settings = _default_settings()
    client = httpx.AsyncClient(
        http2=True,
        # Providers are operator-configured endpoints, so a redirect is suspicious
        # and never followed here; the crawl fetcher performs its own hops.
        follow_redirects=False,
        timeout=Timeouts.defaults().as_httpx(),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
            keepalive_expiry=settings.http_keepalive_expiry,
        ),
        headers={"User-Agent": DEFAULT_USER_AGENT},
        # An inert jar, so cookies only ever flow through the namespaced
        # _GLOBAL_COOKIE_JAR chokepoint.
        cookies=_NoStoreCookieJar(),
    )
    return _bind_to_running_loop(client)


# Superseded loop-bound pooled clients awaiting a loop to close on -- one bounded
# backlog and reaper cover both httpx and curl_cffi kinds.
_PENDING_CLIENT_CLOSE: list[object] = []


_PENDING_CLIENT_CLOSE_MAX = 32


_PENDING_CLIENT_CLOSE_LOCK = threading.Lock()


# Close tasks scheduled from a synchronous context. asyncio holds tasks only
# weakly, so a strong owner keeps them alive; each done callback evicts its entry.
_INFLIGHT_CLIENT_CLOSES: set[asyncio.Future] = set()


def _queue_superseded_close(old_client: Any) -> Any | None:
    """Track one superseded pooled client; return whatever the cap displaced.
    The dedupe check lives inside the cap branch so a re-entrant append+pop
    race cannot pop two distinct entries. The caller releases the displaced object *outside* the lock.
    """
    stale_to_release: Any | None = None
    with _PENDING_CLIENT_CLOSE_LOCK:
        already_queued = old_client in _PENDING_CLIENT_CLOSE
        if not already_queued:
            if len(_PENDING_CLIENT_CLOSE) >= _PENDING_CLIENT_CLOSE_MAX:
                stale_to_release = _PENDING_CLIENT_CLOSE.pop(0)
            _PENDING_CLIENT_CLOSE.append(old_client)
    return stale_to_release


def _release_dropped_client(stale_to_release: Any, *, is_closed: Callable[[Any], bool] | None = None) -> None:
    """Best-effort release of a client the cap displaced, run outside the close lock.
    Sync close() is called directly when present; otherwise the coroutine
    teardown is scheduled on the running loop, or the reference dropped if none runs.
    """
    if stale_to_release is None:
        return
    close = getattr(stale_to_release, "close", None)
    if close is not None and not asyncio.iscoroutinefunction(close):
        with contextlib.suppress(Exception):
            close()
        return
    _schedule_client_close(
        stale_to_release,
        is_closed=_client_is_closed if is_closed is None else is_closed,
    )


async def _drain_pending_client_closes() -> None:
    """Close any superseded pooled clients on the current (live) loop.

    Schedule fires on the *dying* loop, where the task is cancelled before
    aclose completes (every asyncio.run exit), so draining here actually releases the sockets.
    """
    while True:
        with _PENDING_CLIENT_CLOSE_LOCK:
            if not _PENDING_CLIENT_CLOSE:
                return
            client = _PENDING_CLIENT_CLOSE.pop(0)
        await _safe_aclose(client)


def _client_is_closed(client: Any) -> bool:
    """Whether a pooled client reports itself closed, when it can say so -- a
    defensive fallback for callers with no descriptor. An absent is_closed means
    "cannot say", which is NOT closed; this returns False and lets the caller's predicate decide.
    """
    return bool(getattr(client, "is_closed", False))


def _schedule_client_close(client, *, is_closed: Callable[[Any], bool]) -> None:
    """Best-effort asynchronous close of a superseded pooled client: schedule the
    async aclose on the running loop (never block), so sockets don't leak when
    the loop changes. is_closed is required, not inferred -- only the owner knows how a kind reports closed.
    """
    if client is None or is_closed(client):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop: ensure_future would silently build one and strand the
        # coroutine. Declining is honest -- the queued reference closes it.
        return
    if loop.is_closed():
        # A swap racing loop shutdown: create_task would raise into the synchronous
        # swap path. The client is dropped; process teardown reclaims its sockets.
        return
    task = loop.create_task(_safe_aclose(client))
    # asyncio holds tasks weakly, so a teardown scheduled from a synchronous
    # rebuild needs a strong owner; the done callback evicts, bounding the set.
    _INFLIGHT_CLIENT_CLOSES.add(task)
    task.add_done_callback(_INFLIGHT_CLIENT_CLOSES.discard)


class _LoopBoundPooledClient:
    """One pooled client per running loop, released on loop change.
    :meth:`current` reads the module global; :meth:`_supersede` and :meth:`_rebuild`
    implement the three-step lifecycle, and ``is_closed`` is the kind's own close predicate.
    """

    __slots__ = ("build", "consults_settings", "is_closed", "lock", "slot", "namespace")

    def __init__(
        self,
        *,
        slot: str,
        build: Callable[[CrawlerSettings | None], Any],
        is_closed: Callable[[Any], bool],
        lock: threading.Lock,
        consults_settings: bool,
        namespace: dict[str, Any] | None = None,
    ) -> None:
        self.slot = slot
        self.build = build
        self.is_closed = is_closed
        self.lock = lock
        self.consults_settings = consults_settings
        # The pooled instance lives in the module global named by ``slot``; the
        # facade owns that global, so the singleton passes its own globals in.
        self.namespace = globals() if namespace is None else namespace

    # -- pooled instance: the module global named by ``slot`` ----------------

    @property
    def current(self) -> Any:
        """The pooled client as the module global holds it (late-bound)."""
        return self.namespace[self.slot]

    def _set_current(self, value: Any) -> None:
        self.namespace[self.slot] = value

    def _supersede(self, superseded: Any) -> None:
        """Track and best-effort-close a client that is being replaced."""
        _release_dropped_client(_queue_superseded_close(superseded), is_closed=self.is_closed)
        # The descriptor decides reuse-safety for its own kind: a shared probe with
        # "missing means closed" would never close curl_cffi sessions.
        if not self.is_closed(superseded):
            _schedule_client_close(superseded, is_closed=self.is_closed)

    def _rebuild(self, settings: CrawlerSettings | None, superseded: Any) -> Any:
        """Build the replacement, then release the old client. Lock held; a failed build
        propagates. Both kinds are declared deps, so there is no "unavailable" state
        to recover from, and the superseded client is still released on the failing path.
        """
        if settings is None and self.consults_settings:
            # Deferred env walk: paid only when a client is actually built, so
            # operator pool limits are read fresh at every rebuild.
            settings = _default_settings()
        try:
            built = self.build(settings)
        except Exception:
            self._set_current(None)
            if superseded is not None:
                self._supersede(superseded)
            raise
        self._set_current(built)
        if superseded is not None:
            self._supersede(superseded)
        return built

    def get(self, settings: CrawlerSettings | None = None) -> Any:
        """Return the pooled client bound to the current event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self.lock:
            current = self.current
            if current is None or self.is_closed(current):
                # Nothing to release: never built, or already closed by someone
                # else. A closed client owns no live sockets.
                return self._rebuild(settings, None)
            if _loop_bound_to(current) is loop:
                # Hot path: a pooled client alive on the current loop is reused as
                # is; settings are pool config consumed only at construction.
                return current
            return self._rebuild(settings, current)

    async def shutdown(self) -> None:
        """Release the pooled client, and anything still queued, on teardown."""
        await _drain_pending_client_closes()
        current = self.current
        self._set_current(None)
        if current is not None:
            await _safe_aclose(current)


# The two pooled clients, described once and managed by the code above.
_HTTP_CLIENT_SINGLETON = _LoopBoundPooledClient(
    slot="_DEFAULT_HTTP_CLIENT",
    build=_build_async_client,
    is_closed=lambda client: bool(client.is_closed),
    lock=_HTTP_CLIENT_INIT_LOCK,
    consults_settings=True,
)


def get_async_http_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Return the pooled ``httpx.AsyncClient`` bound to the current event loop.
    The client is reused while the running loop is unchanged (HTTP/2 connection
    pooling) and rebuilt if the loop changes (e.g. test harness ``asyncio.run`` sequences).
    """
    return _HTTP_CLIENT_SINGLETON.get(settings)


async def close_async_http_client() -> None:
    """Close the shared async client on service shutdown."""
    await _HTTP_CLIENT_SINGLETON.shutdown()


class _NoStoreCookieJar(_StdlibCookieJar):
    """A cookie jar that never stores or replays anything, so a long-lived pooled
    client cannot leak cookies across tenants (the cffi session's discard_cookies
    closes the same leak). Basing on the ALIASED stdlib jar keeps it inert.
    """

    def extract_cookies(self, response, request=None):
        return None


def _default_settings() -> CrawlerSettings:
    """Get default crawler settings for HTTP client configuration."""
    from app.config.settings import CrawlerSettings

    return CrawlerSettings.from_mapping()


class _LoopBound(Protocol):
    """A pooled client carrying the event loop it was built on, an attribute the
    module installs (neither httpx nor curl_cffi declares it); the type lives
    here once, as a Protocol, rather than a suppression at each site that writes it.
    """

    _bound_loop: asyncio.AbstractEventLoop | None


def _bind_to_running_loop[T](client: T) -> T:
    """Stamp a freshly built pooled client with the loop it was built on -- the
    single reader-safe writer of ``_bound_loop``. None is an answer, not a failure:
    a client built outside a loop has none, so the next ``get`` rebuilds rather than hand out an unusable one.
    """
    bound = cast(_LoopBound, client)
    try:
        bound._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        bound._bound_loop = None
    return client


def _loop_bound_to(client: Any) -> asyncio.AbstractEventLoop | None:
    """The loop a pooled client was built on, or None if never bound. Read with a
    default because the reader must tolerate objects this module didn't build (a
    test double, or a pre-attribute client), yet typed so the comparison is a loop-identity check.
    """
    return getattr(client, "_bound_loop", None)


async def _safe_aclose(client):
    """Await a pooled client's async close, swallowing failures -- the client is
    being replaced (its loop changed), so any close error is irrelevant during
    shutdown. Shared by every kind: httpx ``aclose`` and curl_cffi ``close`` are both coroutines.
    """
    with contextlib.suppress(Exception):
        close = getattr(client, "aclose", None) or client.close
        await close()


T = TypeVar("T")


def _discard_if_unowned_loop(close: Callable[[], Any], slot: str, namespace: dict[str, Any]) -> None:
    """Release ``slot``'s object, closing it only when no loop owns it.

    ``namespace`` is the module holding the pooled object, so one helper serves
    every pool module instead of each keeping a globals()-bound copy.
    """
    if namespace[slot] is None:
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        namespace[slot] = None
        return
    if loop.is_running():
        namespace[slot] = None
    else:
        loop.run_until_complete(close())


def reset_process_state() -> None:
    """Release every process-wide object this module holds.
    The two lazily-created clients and the pending-close queue are one unit: a
    queue holding a released client outlives its loop, so resetting one without the rest leaves a wrong state.
    """
    if _DEFAULT_HTTP_CLIENT is not None:
        _discard_if_unowned_loop(close_async_http_client, "_DEFAULT_HTTP_CLIENT", globals())
    with _PENDING_CLIENT_CLOSE_LOCK:
        _PENDING_CLIENT_CLOSE.clear()


def http_singleton() -> _LoopBoundPooledClient:
    """The pooled httpx descriptor, for builder substitution at the shared seam."""
    return _HTTP_CLIENT_SINGLETON


def has_process_state() -> bool:
    """Whether a lazily-created client or a queued close reference is still held."""
    return _DEFAULT_HTTP_CLIENT is not None or bool(_PENDING_CLIENT_CLOSE)


@contextmanager
def swap_pooled_builders(swaps: list[tuple[Any, Callable[[CrawlerSettings | None], Any]]]) -> Iterator[None]:
    """Rebind the given pooled singletons' builders, restored on the way out.

    The builders are the one part of the pools a caller can legitimately
    supply; the previous builder returns when the block exits, even on error.
    """
    saved = [(singleton, singleton.build) for singleton, _ in swaps]
    for singleton, builder in swaps:
        singleton.build = builder
    try:
        yield
    finally:
        for singleton, original in saved:
            singleton.build = original
