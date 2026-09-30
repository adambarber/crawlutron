"""Owns pooled HTTP and curl_cffi client lifecycles.

This module owns construction, loop binding, supersession, bounded close queues,
and teardown. It must not assemble request policy, fetch responses, or own crawl results.

THE SUPERSESSION LIFECYCLE
--------------------------
``httpx.AsyncClient`` and curl_cffi's ``AsyncSession`` are pooled the same way:
built once for the running loop, reused while that loop lives, rebuilt when the
loop changes. The rebuild is the dangerous step -- the object being replaced
owns sockets on a loop that is already gone, and releasing it is asynchronous.
:class:`_LoopBoundPooledClient` implements the only reliable pattern:

  1. hand the superseded client to the bounded, deduped pending-close queue
     (:func:`_queue_superseded_close`) so the reference is tracked even with
     no loop running, and release whatever the cap displaced
     (:func:`_release_dropped_client`) -- a synchronous close if that kind
     publishes one, otherwise a close scheduled on the running loop;
  2. also schedule a best-effort close on the current loop
     (:func:`_schedule_client_close`) for the case where the rebuild happens
     on a live loop;
  3. let the next fetch on a live loop drain the queue
     (:func:`_drain_pending_client_closes`) -- that drain is what actually
     releases the sockets once step 2 loses the race with loop shutdown.

Steps 1 and 2 are gated by the descriptor's ``is_closed`` predicate, never by
probing an attribute name: only the owning kind knows how it reports being
finished, and curl_cffi's session reports it in no way at all.

Both pooled singletons are descriptors of this one lifecycle so they cannot
drift apart again: the cffi copy stopped at step 2 and leaked one TLS session
per loop change. The class docstring states the contract; the narrative lives
here because the reader who needs it is looking for the mechanism, not the
attribute list.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from http.cookiejar import CookieJar as _StdlibCookieJar
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

import curl_cffi.requests as cffi_requests
import httpx

from app.constants import CFFI_IMPERSONATE_TARGET, DEFAULT_USER_AGENT
from app.timeouts import Timeouts

if TYPE_CHECKING:
    from app.settings import CrawlerSettings


_DEFAULT_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_INIT_LOCK = threading.Lock()


def _default_settings() -> CrawlerSettings:
    """Get default crawler settings for HTTP client configuration."""
    from app.settings import CrawlerSettings

    return CrawlerSettings.from_mapping()


class _NoStoreCookieJar(_StdlibCookieJar):
    """A cookie jar that never stores or replays anything (C2).

    The long-lived httpx client would otherwise accumulate every Set-Cookie and
    replay domain-matching cookies on later requests independently of the
    namespaced ``_GLOBAL_COOKIE_JAR`` -- the same cross-tenant leak the cffi
    session's ``discard_cookies`` closes. httpx has no such switch and its
    ``Client.cookies`` property wraps any assigned value in a fresh
    ``httpx.Cookies``, so the jar must be inert at construction:
    ``httpx.Cookies`` adopts a passed-in ``CookieJar`` as its own ``jar``
    rather than copying it, which is why this override survives the wrap. With
    nothing stored there is nothing to replay and nothing to clobber the
    namespaced ``Cookie:`` header the fetcher sets per request.

    The base is deliberately the ALIASED stdlib jar (``_StdlibCookieJar``), not
    the project's own ``CookieJar`` defined further down this module:
    this class is defined first, so its base resolves from whatever the
    ``CookieJar`` name points at at class-creation time. Basing it on the alias
    makes that resolution independent of source order -- rebinding the base to
    the storing jar below would silently invert this class's purpose, and the
    shadowing was load-bearing.
    """

    def extract_cookies(self, response, request=None):
        return None


T = TypeVar("T")


class _LoopBound(Protocol):
    """A pooled client carrying the event loop it was built on.

    Neither ``httpx.AsyncClient`` nor curl_cffi's ``AsyncSession`` declares this
    attribute -- this module installs it -- so its type is declared here, once,
    rather than left to be implied by a suppression at each site that writes it.
    A ``Protocol`` is the honest shape for an attribute added to somebody else's
    object: it states the contract this module depends on without claiming the
    library publishes it.
    """

    _bound_loop: asyncio.AbstractEventLoop | None


def _bind_to_running_loop(client: T) -> T:
    """Stamp a freshly built pooled client with the loop it was built on.

    The single place that writes ``_bound_loop``. It was four: two builders, each
    with its own no-running-loop branch, and each of the four carrying a bare
    ``# type: ignore[attr-defined]`` for an attribute neither library declares.
    Four suppressions for one fact about one attribute, read through a fifth
    defaulted ``getattr`` -- and none of them said what the attribute holds or
    when it is None.

    None is an answer, not a failure. A client built outside a loop has no loop
    to belong to, and :func:`_loop_bound_to` reports that as matching nothing, so
    the next ``get`` rebuilds rather than handing out a client this loop cannot
    use.
    """
    bound = cast(_LoopBound, client)
    try:
        bound._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        bound._bound_loop = None
    return client


def _loop_bound_to(client: Any) -> asyncio.AbstractEventLoop | None:
    """The loop a pooled client was built on, or None if it was never bound.

    A defaulted read rather than an attribute access, because this is the reader
    that has to tolerate an object this module did not build -- a test double, or
    a client predating the attribute. Typed anyway: the declared return is what
    makes the caller's comparison a loop-identity check instead of a comparison
    against ``Any``.
    """
    return getattr(client, "_bound_loop", None)


def _build_async_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with configurable connection pool."""
    if settings is None:
        settings = _default_settings()
    client = httpx.AsyncClient(
        http2=True,
        # Providers are operator-configured endpoints; any redirect must be
        # treated as suspicious rather than silently followed (no per-hop
        # SafeUrl re-validation on this client). The crawl fetcher does its
        # own manual hops and never relies on this flag.
        follow_redirects=False,
        timeout=Timeouts.defaults().as_httpx(),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
            keepalive_expiry=settings.http_keepalive_expiry,
        ),
        headers={"User-Agent": DEFAULT_USER_AGENT},
        # C2: an inert jar, so cookies only ever flow through the namespaced
        # _GLOBAL_COOKIE_JAR chokepoint.
        cookies=_NoStoreCookieJar(),
    )
    return _bind_to_running_loop(client)


async def _safe_aclose(client):
    """Await a pooled client's async close, swallowing failures.

    The client is being replaced (its event loop changed); any close error is
    irrelevant, so we ignore it rather than risk surfacing during shutdown.
    Shared by every pooled kind: httpx exposes ``aclose``, curl_cffi exposes
    ``close`` -- both coroutine methods, so one helper reaps either.
    """
    with contextlib.suppress(Exception):
        close = getattr(client, "aclose", None) or client.close
        await close()


# Superseded loop-bound pooled clients awaiting a live event loop to be closed
# on. Shared by every pooled kind (httpx.AsyncClient and curl_cffi.AsyncSession
# are the same resource: a connection pool bound to one loop), so one bounded
# backlog and one reaper cover both.
# Bounded so a stream of superseded clients cannot grow the list without
# bound if drains are delayed.
_PENDING_CLIENT_CLOSE: list[Any] = []
_PENDING_CLIENT_CLOSE_MAX = 32
_PENDING_CLIENT_CLOSE_LOCK = threading.Lock()

# Close tasks scheduled from synchronous context. asyncio keeps only a weak
# reference to a running task, so without a strong owner a teardown scheduled
# during a lock-held rebuild can be garbage collected before it runs to
# completion. Entries are evicted by each task's own done callback.
_INFLIGHT_CLIENT_CLOSES: set[asyncio.Future] = set()


def _queue_superseded_close(old_client: Any) -> Any | None:
    """Track one superseded pooled client; return whatever the cap displaced.

    The dedupe check lives inside the cap branch so a re-entrant append+pop
    race cannot pop two distinct entries. Returns the displaced object for the
    caller to release *outside* the lock.
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
    """Best-effort release of a client the cap had to displace.

    Runs outside ``_PENDING_CLIENT_CLOSE_LOCK`` so a potentially slow
    socket-close cannot block other clients entering that critical section.

    What this actually does, per context:

      * a synchronous ``close()`` is called directly when the kind publishes one
        (neither pooled kind does today, but a future one may);
      * otherwise, when an event loop is running, the async teardown is
        scheduled on it through :func:`_schedule_client_close`, so the sockets
        really are released instead of the reference being abandoned. Pass the
        owning descriptor's ``is_closed`` so the same kind decides here as it
        does on the eager path; without a descriptor the check falls back to
        "closed only if the client says so".
      * otherwise (no loop running) there is nothing that can run a coroutine,
        so the reference is dropped and GC reclaims it once the owning loop is
        gone. That is the only case that still degrades to a drop, and it is
        reachable only when the cap fires from synchronous context.

    Both pooled kinds expose only a coroutine teardown -- ``httpx.AsyncClient``
    publishes ``aclose`` and no ``close``, and ``curl_cffi``'s ``AsyncSession.
    close`` is itself a coroutine function -- so the earlier "synchronous close
    that works from a dying loop" claim was not true of either: the guard below
    used to reject coroutine closes and return, making the whole function a
    no-op. Scheduling is safe here (unlike awaiting): the displaced client is no
    longer reachable from the queue, and a scheduled-but-cancelled teardown is
    still reclaimed by GC exactly as a dropped reference was.

    Note this path is shared across kinds: because one queue holds both pooled
    kinds under one cap, cffi churn can displace a queued httpx client and vice
    versa. Each displaced client still gets the treatment above, so cross-kind
    eviction cannot orphan a socket -- but a burst of one kind does crowd out
    how many of the other can wait for a drain, which is why the release here is
    real rather than a reference drop.
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

    ``_schedule_client_close`` fires on the *dying* loop, where the scheduled
    task is cancelled before ``aclose`` completes (every ``asyncio.run`` exit).
    Draining here, inside a fetch that is already running on a live loop,
    actually releases those sockets instead of leaking them. A bounded
    queue with cap-on-enqueue keeps the list from growing unboundedly under
    a stream of rapid loop changes.
    """
    while True:
        with _PENDING_CLIENT_CLOSE_LOCK:
            if not _PENDING_CLIENT_CLOSE:
                return
            client = _PENDING_CLIENT_CLOSE.pop(0)
        await _safe_aclose(client)


def _client_is_closed(client: Any) -> bool:
    """Whether a pooled client reports itself closed, when it can say so.

    Only used as a last-resort defensive check for callers that have no
    descriptor to ask. An absent ``is_closed`` means "this kind cannot say",
    which is NOT evidence of being closed -- treating it as such is what
    silently disabled the eager close for every curl_cffi session (D2). So this
    returns False for unknown shapes and lets the caller's own predicate decide.
    """
    return bool(getattr(client, "is_closed", False))


def _schedule_client_close(client, *, is_closed: Callable[[Any], bool]) -> None:
    """Best-effort asynchronous close of a superseded pooled HTTP client.

    ``get_async_http_client`` runs within an event loop, so we schedule the
    async ``aclose`` on the running loop instead of blocking. This prevents
    leaking the old client's sockets/file descriptors when the loop changes
    (e.g. repeated ``asyncio.run`` calls).

    ``is_closed`` is required rather than inferred: only the descriptor that
    owns a pooled kind knows how that kind reports being closed (httpx has a
    flag, curl_cffi has nothing). Defaulting the question to "no attribute
    means closed" made every session look already finished and skipped this
    call entirely.
    """
    if client is None or is_closed(client):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop to schedule on. ``asyncio.ensure_future`` would not
        # raise here -- with no current loop it silently builds one, parks the
        # task on it, and leaves a coroutine that is never awaited (a
        # DeprecationWarning plus an "Exception ignored in: <coroutine object
        # _safe_aclose>" at GC). Declining to schedule is the honest no-op: the
        # queued reference is what guarantees the eventual close.
        return
    if loop.is_closed():
        # The swap raced a loop that is shutting down: ``create_task`` would
        # raise "Event loop is closed" into the synchronous swap path. The
        # client is dropped instead -- process teardown reclaims its sockets.
        return
    task = loop.create_task(_safe_aclose(client))
    # asyncio keeps only a weak reference to a running task, so a teardown
    # scheduled from a synchronous rebuild could be garbage collected before it
    # finished -- reproducing the leak this function exists to prevent. The done
    # callback is what evicts again, so this set cannot grow without bound.
    _INFLIGHT_CLIENT_CLOSES.add(task)
    task.add_done_callback(_INFLIGHT_CLIENT_CLOSES.discard)


class _LoopBoundPooledClient:
    """One pooled HTTP client per running event loop, released on loop change.

    The contract, not the narrative: :meth:`current` is the pooled client as the
    module global holds it, :meth:`_supersede` and :meth:`_rebuild` implement the
    three-step supersession lifecycle documented at module level, and both
    pooled singletons (httpx and curl_cffi) are descriptors of this one class so
    they cannot drift apart.

    ``is_closed`` is the owning kind's own predicate, consulted for every
    decision about whether a client still needs closing. ``build`` is a
    late-bound lambda rather than a called symbol so a test that monkeypatches
    ``_build_async_client`` or ``_build_cffi_session`` is still observed; the
    pooled instance lives in the module global named by ``slot`` for the same
    reason, since service teardown and the suites reset pooled state by
    assigning that global.
    """

    __slots__ = ("build", "consults_settings", "is_closed", "lock", "slot")

    def __init__(
        self,
        *,
        slot: str,
        build: Callable[[CrawlerSettings | None], Any],
        is_closed: Callable[[Any], bool],
        lock: threading.Lock,
        consults_settings: bool,
    ) -> None:
        self.slot = slot
        self.build = build
        self.is_closed = is_closed
        self.lock = lock
        self.consults_settings = consults_settings

    # -- pooled instance: the module global named by ``slot`` ----------------

    @property
    def current(self) -> Any:
        """The pooled client as the module global holds it (late-bound)."""
        return globals()[self.slot]

    def _set_current(self, value: Any) -> None:
        globals()[self.slot] = value

    def _supersede(self, superseded: Any) -> None:
        """Track and best-effort-close a client that is being replaced."""
        _release_dropped_client(_queue_superseded_close(superseded), is_closed=self.is_closed)
        # The descriptor decides reuse-safety for its own kind: curl_cffi's
        # session publishes no ``is_closed`` flag, so a shared attribute probe
        # with a "missing means closed" default would classify every session as
        # already finished and never close it eagerly.
        if not self.is_closed(superseded):
            _schedule_client_close(superseded, is_closed=self.is_closed)

    def _rebuild(self, settings: CrawlerSettings | None, superseded: Any) -> Any:
        """Build the replacement, then release the old client. Lock held.

        A failed build propagates. Both clients described by this class are
        declared dependencies (``httpx``, ``curl_cffi``), so there is no
        "unavailable" state to recover from, and the ``except ImportError:
        return None`` that used to stand here was worse than useless: it
        returned a ``None`` that no httpx caller checks -- so a missing
        mandatory package surfaced as an ``AttributeError`` inside a fetcher
        rather than an import error at start-up -- while the cffi path read the
        same ``None`` as "impersonation unavailable" and silently downgraded a
        TLS-fingerprinted fetch to plain httpx, reporting success.

        The superseded client is still released on the failing path. A rebuild
        that raised must not leave the client it was replacing with no owner,
        which is exactly the socket leak this descriptor exists to prevent.
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
                # Hot path (perf review 1.1): the pooled client is alive on the
                # current loop, so no settings are consulted at all. Settings
                # are pool configuration consumed only at construction; a
                # reused client carries the limits it was built with.
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
    build=lambda settings: _build_async_client(settings),
    is_closed=lambda client: bool(client.is_closed),
    lock=_HTTP_CLIENT_INIT_LOCK,
    consults_settings=True,
)


def get_async_http_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Return the pooled ``httpx.AsyncClient`` bound to the current event loop.

    The client is reused while the running event loop is unchanged (HTTP/2
    connection pooling), and transparently rebuilt if the loop changes (for
    example when a test harness drives many ``asyncio.run`` calls in sequence).
    """
    return _HTTP_CLIENT_SINGLETON.get(settings)


async def close_async_http_client() -> None:
    """Close the shared async client on service shutdown."""
    await _HTTP_CLIENT_SINGLETON.shutdown()


# Shared curl_cffi TLS-impersonation session (one per running event loop).
# A fresh AsyncSession per request rebuilt TLS state and the connection pool
# every time; sharing it keeps warm connections and the impersonated
# fingerprint across requests. Mirrors
# get_async_http_client, including the loop-change rebuild.
_DEFAULT_CFFI_SESSION: Any | None = None
_CFFI_SESSION_INIT_LOCK = threading.Lock()


def _build_cffi_session() -> Any:
    """Build a shared curl_cffi AsyncSession (impersonation fixed, no timeout).

    The per-request timeout travels on each ``get()`` call instead of the
    session so one session serves every policy budget.
    """
    # discard_cookies=True (C2): the session ships a requests-compatible jar
    # that would otherwise absorb every Set-Cookie and replay domain-matching
    # cookies on later requests INDEPENDENTLY of the namespaced
    # _GLOBAL_COOKIE_JAR -- the exact cross-tenant leak cookie_namespace exists
    # to prevent -- and a replayed jar can also clobber the namespaced
    # Cookie: header the fetcher builds per request. With discarding on, the
    # jar is born empty and stays empty, so cookies flow only through the
    # namespaced chokepoint: out via _cookie_header_for_url, in via
    # _store_set_cookies. Sending is unaffected; curl still honors the Cookie
    # header we set explicitly.
    session: cffi_requests.AsyncSession = cffi_requests.AsyncSession(
        impersonate=CFFI_IMPERSONATE_TARGET, discard_cookies=True
    )
    return _bind_to_running_loop(session)


_CFFI_SESSION_SINGLETON = _LoopBoundPooledClient(
    slot="_DEFAULT_CFFI_SESSION",
    build=lambda settings: _build_cffi_session(),
    # AsyncSession publishes no is_closed flag and nothing else closes it in
    # process, so only the loop identity decides whether it is reusable.
    is_closed=lambda session: False,
    lock=_CFFI_SESSION_INIT_LOCK,
    # The session carries no pool limits (the per-request timeout travels on
    # each get()), so a rebuild must not pay a settings/env walk.
    consults_settings=False,
)


def get_cffi_session() -> Any:
    """Return the shared curl_cffi session bound to the current event loop.

    Reused while the running loop is unchanged (warm TLS/connection pool),
    transparently rebuilt when the loop changes. A build that fails raises:
    curl_cffi is a declared dependency, so there is no missing-package state to
    return ``None`` for. The ``Any`` return is not looseness -- curl_cffi ships
    partial stubs and the session type is not expressible without them, which is
    why the impersonation target and cookie-jar discipline around it are
    enforced by the code that builds it rather than by a signature.

    A session superseded by a loop change is tracked in the shared bounded
    pending-close queue and drained by the next fetch on a live loop -- the
    same discipline the pooled httpx client has, which this path used to
    approximate with a best-effort task on the loop that was already exiting.
    """
    return _CFFI_SESSION_SINGLETON.get()


async def close_cffi_session() -> None:
    """Close the shared curl_cffi session on service shutdown / test reset."""
    await _CFFI_SESSION_SINGLETON.shutdown()
