"""Pooled client lifecycle shared by the HTTP facade: build once per loop, supersede cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.cookiejar import CookieJar as _StdlibCookieJar
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

import httpx

from crawlutron.runtime import process_state
from crawlutron.runtime.resource_lifecycle import DeferredClose
from crawlutron.transport.pooled_client_release import (
    PENDING_CLIENT_CLOSE,
    PENDING_CLIENT_CLOSE_LOCK,
    PooledClient,
    close_on_fresh_loop,
    close_on_thread,
    discard_if_unowned_loop,
    drain_pending_client_closes,
    safe_aclose,
)
from crawlutron.transport.timeouts import Timeouts
from crawlutron.vocabulary.identity import DEFAULT_USER_AGENT

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings


PooledClientBuilder = Callable[["CrawlerSettings | None"], PooledClient]

__all__ = [
    "LoopBoundPooledClient",
    "PENDING_CLIENT_CLOSE_MAX",
    "PooledClientBuilder",
    "T",
    "bind_to_running_loop",
    "close_async_http_client",
    "get_async_http_client",
    "http_singleton",
    "swap_pooled_builders",
]

_DEFAULT_HTTP_CLIENT: httpx.AsyncClient | None = None

# Dirty flag: a pooled client was built since the flag was last reset; the
# harness's per-test reset reads it via the public ``clients_were_built`` door.
_CLIENTS_BUILT: bool = False


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
    return bind_to_running_loop(client)


# Superseded loop-bound pooled clients awaiting a loop to close on -- one bounded
# backlog and reaper cover both httpx and curl_cffi kinds.

PENDING_CLIENT_CLOSE_MAX = 32


def _queue_superseded_close(old_client: PooledClient) -> PooledClient | None:
    """Track one superseded pooled client; return whatever the cap displaced.
    The dedupe check lives inside the cap branch so a re-entrant append+pop
    race cannot pop two distinct entries. The caller releases the displaced object *outside* the lock.
    """
    stale_to_release: PooledClient | None = None
    with PENDING_CLIENT_CLOSE_LOCK:
        already_queued = old_client in PENDING_CLIENT_CLOSE
        if not already_queued:
            if len(PENDING_CLIENT_CLOSE) >= PENDING_CLIENT_CLOSE_MAX:
                stale_to_release = PENDING_CLIENT_CLOSE.pop(0)
            PENDING_CLIENT_CLOSE.append(old_client)
    return stale_to_release


def _release_dropped_client(
    stale_to_release: PooledClient | None,
    *,
    is_closed: Callable[[PooledClient], bool] | None = None,
) -> None:
    """Best-effort release of a client the cap displaced, run outside the close lock.
    Sync close() is called directly when present; otherwise the coroutine
    teardown is scheduled through the shared owner, or the reference dropped if none runs.
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


def _client_is_closed(client: PooledClient) -> bool:
    """Whether a pooled client reports itself closed, when it can say so -- a
    defensive fallback for callers with no descriptor. An absent is_closed means
    "cannot say", which is NOT closed; this returns False and lets the caller's predicate decide.
    """
    return bool(getattr(client, "is_closed", False))


def _schedule_client_close(client: PooledClient | None, *, is_closed: Callable[[PooledClient], bool]) -> None:
    """Best-effort async close of a superseded pooled client through the shared
    owner; the caller keeps the predicate, the owner keeps the mechanism."""
    if client is None or is_closed(client):
        return
    _SUPERSEDED_CLOSE.schedule(client, skip_if_closed=False)


class LoopBoundPooledClient:
    """One pooled client per running loop, released on loop change.
    :meth:`current` reads the module global; :meth:`_supersede` and :meth:`_rebuild`
    implement the three-step lifecycle, and ``is_closed`` is the kind's own close predicate.
    """

    __slots__ = ("build", "consults_settings", "is_closed", "lock", "slot", "namespace")

    def __init__(
        self,
        *,
        slot: str,
        build: PooledClientBuilder,
        is_closed: Callable[[PooledClient], bool],
        lock: threading.Lock,
        consults_settings: bool,
        namespace: dict[str, PooledClient | None] | None = None,
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
    def current(self) -> PooledClient | None:
        """The pooled client as the module global holds it (late-bound)."""
        return cast(PooledClient | None, self.namespace[self.slot])

    def _set_current(self, value: PooledClient | None) -> None:
        self.namespace[self.slot] = value

    def _supersede(self, superseded: PooledClient) -> None:
        """Track and best-effort-close a client that is being replaced."""
        _release_dropped_client(_queue_superseded_close(superseded), is_closed=self.is_closed)
        # The descriptor decides reuse-safety for its own kind: a shared probe with
        # "missing means closed" would never close curl_cffi sessions.
        if not self.is_closed(superseded):
            _schedule_client_close(superseded, is_closed=self.is_closed)

    def _rebuild(self, settings: CrawlerSettings | None, superseded: PooledClient | None) -> PooledClient:
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
        self._mark_built()
        if superseded is not None:
            self._supersede(superseded)
        return built

    def clients_were_built(self) -> bool:
        """Whether a pooled client was built since the flag was last reset."""
        return _CLIENTS_BUILT

    def reset_clients_built_flag(self) -> None:
        """Clear the dirty flag so the next test starts clean."""
        global _CLIENTS_BUILT
        _CLIENTS_BUILT = False

    def _mark_built(self) -> None:
        global _CLIENTS_BUILT
        _CLIENTS_BUILT = True

    def get(self, settings: CrawlerSettings | None = None) -> PooledClient:
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
        """Release the pooled client and the queued superseded clients: the
        queue MUST empty here, or a sync reset later sees it non-empty and
        tries to host a fresh loop on a thread whose loop still runs."""
        while True:
            with PENDING_CLIENT_CLOSE_LOCK:
                if not PENDING_CLIENT_CLOSE:
                    break
                client = PENDING_CLIENT_CLOSE.pop(0)
            await safe_aclose(client)
        current = self.current
        self._set_current(None)
        if current is not None:
            await safe_aclose(current)


# The two pooled clients, described once and managed by the code above.
_HTTP_CLIENT_SINGLETON = LoopBoundPooledClient(
    slot="_DEFAULT_HTTP_CLIENT",
    build=cast(PooledClientBuilder, _build_async_client),
    is_closed=lambda client: bool(getattr(client, "is_closed", False)),
    lock=_HTTP_CLIENT_INIT_LOCK,
    consults_settings=True,
)


def get_async_http_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Return the pooled ``httpx.AsyncClient`` bound to the current event loop.
    The client is reused while the running loop is unchanged (HTTP/2 connection
    pooling) and rebuilt if the loop changes (e.g. test harness ``asyncio.run`` sequences).
    """
    pooled = _HTTP_CLIENT_SINGLETON.get(settings)
    # Validated boundary: this descriptor's builder is _build_async_client, so the
    # pooled object is always the httpx client the cast names.
    return cast(httpx.AsyncClient, pooled)


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
    from crawlutron.config.settings import CrawlerSettings

    return CrawlerSettings.from_mapping()


class _LoopBound(Protocol):
    """A pooled client carrying the event loop it was built on, an attribute the
    module installs (neither httpx nor curl_cffi declares it); the type lives
    here once, as a Protocol, rather than a suppression at each site that writes it.
    """

    _bound_loop: asyncio.AbstractEventLoop | None


def bind_to_running_loop[T](client: T) -> T:
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


def _loop_bound_to(client: object) -> asyncio.AbstractEventLoop | None:
    """The loop a pooled client was built on, or None if never bound. Read with a
    default because the reader must tolerate objects this module didn't build (a
    test double, or a pre-attribute client), yet typed so the comparison is a loop-identity check.
    """
    return getattr(client, "_bound_loop", None)


_SUPERSEDED_CLOSE: DeferredClose[PooledClient] = DeferredClose(aclose=safe_aclose)

T = TypeVar("T")


def _reset() -> None:
    """Release every process-wide object this module holds.
    The two lazily-created clients and the pending-close queue are one unit: a
    queue holding a released client outlives its loop, so resetting one without the rest leaves a wrong state.
    """
    from crawlutron.transport.pooled_client_release import (
        clear_pending_client_closes,
    )
    from crawlutron.transport.pooled_socket_abort import abort_transport_sockets

    if _DEFAULT_HTTP_CLIENT is not None:
        bound = _loop_bound_to(_DEFAULT_HTTP_CLIENT)
        if bound is not None and bound.is_closed():
            # The owning loop is dead: no loop can complete its transports'
            # close, so abort the sockets directly (the unraisable __del__ family).
            abort_transport_sockets((bound,))
        discard_if_unowned_loop(close_async_http_client, "_DEFAULT_HTTP_CLIENT", globals(), reset=True)
    # Failed rebuild can leave current=None with a queued superseded client;
    # close it instead of dropping it (socket-leak family).
    with PENDING_CLIENT_CLOSE_LOCK:
        queued = bool(PENDING_CLIENT_CLOSE)
    if queued:
        try:
            running = asyncio.get_running_loop() is not None
        except RuntimeError:
            running = False
        if running:
            close_on_thread(drain_pending_client_closes)
        else:
            close_on_fresh_loop(drain_pending_client_closes)
    clear_pending_client_closes()


def http_singleton() -> LoopBoundPooledClient:
    """The pooled httpx descriptor, for builder substitution at the shared seam."""
    return _HTTP_CLIENT_SINGLETON


def _has() -> bool:
    """Whether a lazily-created client or a queued close reference is still held."""
    return _DEFAULT_HTTP_CLIENT is not None or bool(PENDING_CLIENT_CLOSE)


process_state.register(__name__, _reset, _has)


@contextmanager
def swap_pooled_builders(
    swaps: list[tuple[LoopBoundPooledClient, PooledClientBuilder]],
) -> Generator[None, None, None]:
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
