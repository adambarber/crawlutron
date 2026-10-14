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
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import curl_cffi.requests as cffi_requests

from app.transport.pooled_clients import (
    _DEFAULT_HTTP_CLIENT,
    _HTTP_CLIENT_SINGLETON,
    _PENDING_CLIENT_CLOSE,
    _PENDING_CLIENT_CLOSE_LOCK,
    _PENDING_CLIENT_CLOSE_MAX,
    _build_async_client,
    _drain_pending_client_closes,
    _LoopBoundPooledClient,
    _NoStoreCookieJar,
    _queue_superseded_close,
    _release_dropped_client,
    _schedule_client_close,
    close_async_http_client,
    get_async_http_client,
)
from app.vocabulary.strategies import CFFI_IMPERSONATE_TARGET

__all__ = [
    "_LoopBoundPooledClient",
    "_NoStoreCookieJar",
    "_PENDING_CLIENT_CLOSE",
    "_PENDING_CLIENT_CLOSE_LOCK",
    "_PENDING_CLIENT_CLOSE_MAX",
    "_build_async_client",
    "_drain_pending_client_closes",
    "_queue_superseded_close",
    "_release_dropped_client",
    "_schedule_client_close",
    "close_async_http_client",
    "get_async_http_client",
]


from app.transport.pooled_clients import _bind_to_running_loop

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings


# One TLS-impersonation session per event loop, mirroring
# get_async_http_client (loop-change rebuild included), keeping connections warm.
_DEFAULT_CFFI_SESSION: Any | None = None


_CFFI_SESSION_INIT_LOCK = threading.Lock()


def _build_cffi_session(settings: CrawlerSettings | None = None) -> Any:
    """Build a shared curl_cffi AsyncSession (impersonation fixed, no timeout).

    The per-request timeout travels on each ``get()`` call instead of the
    session so one session serves every policy budget.

    ``settings`` is accepted and ignored so the builder matches the
    ``build(settings)`` contract both pooled singletons share. It is not
    decoration: an httpx client is configured from the operator's pool limits and
    a curl_cffi session is not, so the shape that carries them is not the shape
    that needs them. Saying so in the signature is what lets this be a direct
    reference rather than a lambda discarding its argument.
    """
    # discard_cookies=True: the built-in jar would replay cookies outside the
    # namespaced chokepoint -- a cross-tenant leak -- so it is born empty.
    session: cffi_requests.AsyncSession = cffi_requests.AsyncSession(
        impersonate=CFFI_IMPERSONATE_TARGET, discard_cookies=True
    )
    return _bind_to_running_loop(session)


_CFFI_SESSION_SINGLETON = _LoopBoundPooledClient(
    slot="_DEFAULT_CFFI_SESSION",
    build=_build_cffi_session,
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


def _discard_if_unowned_loop(close: Callable[[], Any], slot: str) -> None:
    """Release ``slot``'s object, closing it only when no loop owns it.

    Both process-wide clients are created lazily on whichever event loop first
    asks, and a client belonging to a loop that has already finished cannot be
    awaited from another one. So the only safe teardown without an await is to drop
    the reference and let the finished loop's owner reclaim it -- which is why this
    splits on ``is_running()`` rather than always closing.
    """
    if globals()[slot] is None:
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        globals()[slot] = None
        return
    if loop.is_running():
        globals()[slot] = None
    else:
        loop.run_until_complete(close())


def reset_process_state() -> None:
    """Release every process-wide object this module holds.

    The two lazily-created clients and the pending-close queue are one unit: a
    queue holding references to a client that has been released outlives the loop
    that could have drained it, so resetting one without the others leaves a
    state production never reaches.
    """
    _discard_if_unowned_loop(close_async_http_client, "_DEFAULT_HTTP_CLIENT")
    _discard_if_unowned_loop(close_cffi_session, "_DEFAULT_CFFI_SESSION")
    # A sync context cannot await aclose; dropping the queue lets GC reclaim the
    # sockets once the loop that owns them exits.
    with _PENDING_CLIENT_CLOSE_LOCK:
        _PENDING_CLIENT_CLOSE.clear()


def has_process_state() -> bool:
    """Whether a lazily-created client or a queued close reference is still held."""
    return (
        _DEFAULT_HTTP_CLIENT is not None or _DEFAULT_CFFI_SESSION is not None or bool(_PENDING_CLIENT_CLOSE)
    )


@contextmanager
def pooled_client_builders(
    *,
    http: Callable[[CrawlerSettings | None], Any] | None = None,
    cffi: Callable[[CrawlerSettings | None], Any] | None = None,
) -> Iterator[None]:
    """Supply the pooled clients' builders for the duration of a block.

    The pooled clients are process-wide and built lazily on whichever loop asks
    first, so no caller -- production or test -- ever stands at their
    construction site. The builder is therefore the one part of the pool a
    caller can legitimately supply, and this is the argument for it. It replaces
    rebinding ``_build_async_client`` or ``_build_cffi_session`` on the module,
    which is how these were substituted before: a name the module keeps for its
    own use, reached from outside to redirect a process-wide object, and
    restored by nothing but the test's own teardown.

    Omitting a keyword leaves that pool's builder alone, so a block can
    substitute one kind without rebuilding the other's identity. The previous
    builder is restored on the way out *including* when the body raises. That
    restore is the point rather than a convenience: the pools outlive the block
    and every later caller in the process would otherwise inherit the double.
    """
    swaps = [
        (singleton, builder)
        for singleton, builder in ((_HTTP_CLIENT_SINGLETON, http), (_CFFI_SESSION_SINGLETON, cffi))
        if builder is not None
    ]
    saved = [(singleton, singleton.build) for singleton, _ in swaps]
    for singleton, builder in swaps:
        singleton.build = builder
    try:
        yield
    finally:
        for singleton, original in saved:
            singleton.build = original
