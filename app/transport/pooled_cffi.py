"""The pooled curl_cffi session: one per event loop, superseded through the shared queue.

The canonical singleton lives with its own slot namespace; the shared lifecycle
descriptors and bounded close queue live in :mod:`app.transport.pooled_clients`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import curl_cffi.requests as cffi_requests

from app.transport.pooled_clients import (
    _bind_to_running_loop,
    _discard_if_unowned_loop,
    _LoopBoundPooledClient,
)

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings
from app.vocabulary.strategies import CFFI_IMPERSONATE_TARGET

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
    # The pooled instance lives in a global of THIS module, so this
    # descriptor's slot resolves against it rather than the base module.
    namespace=globals(),
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


def own_session() -> Any:
    """The pooled session, for the shared pool's process-state decisions."""
    return _DEFAULT_CFFI_SESSION


def owns_process_state() -> bool:
    """Whether a cffi session is currently held process-wide."""
    return _DEFAULT_CFFI_SESSION is not None


def close_queued_session() -> None:
    """Release the pooled session, closing it only when no loop owns it."""
    _discard_if_unowned_loop(close_cffi_session, "_DEFAULT_CFFI_SESSION", globals())


def session_singleton() -> _LoopBoundPooledClient:
    """The pooled descriptor, for builder substitution at the shared seam."""
    return _CFFI_SESSION_SINGLETON
