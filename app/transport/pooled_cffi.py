"""The pooled curl_cffi session: one per event loop, superseded through the shared queue.

The canonical singleton lives with its own slot namespace; the shared lifecycle
descriptors and bounded close queue live in :mod:`app.transport.pooled_clients`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

import curl_cffi.requests as cffi_requests

from app.transport.pooled_clients import (
    LoopBoundPooledClient,
    PooledClientBuilder,
    bind_to_running_loop,
    discard_if_unowned_loop,
)

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings

from app.vocabulary.strategies import CFFI_IMPERSONATE_TARGET

#: Runtime alias: annotations name the session type, and tests import this
#: module directly, so the alias must exist at runtime too.
CffiSession = cffi_requests.AsyncSession

_DEFAULT_CFFI_SESSION: CffiSession | None = None


_CFFI_SESSION_INIT_LOCK = threading.Lock()


def _build_cffi_session(settings: CrawlerSettings | None = None) -> CffiSession:
    """Build a shared curl_cffi AsyncSession (impersonation fixed, no timeout --
    the timeout rides each ``get()`` so one session serves every budget); ``settings``
    is accepted and ignored to match the shared ``build(settings)`` contract.
    """
    # discard_cookies=True: the built-in jar would replay cookies outside the
    # namespaced chokepoint -- a cross-tenant leak -- so it is born empty.
    session: cffi_requests.AsyncSession = cffi_requests.AsyncSession(
        impersonate=CFFI_IMPERSONATE_TARGET, discard_cookies=True
    )
    return bind_to_running_loop(session)


_CFFI_SESSION_SINGLETON = LoopBoundPooledClient(
    slot="_DEFAULT_CFFI_SESSION",
    # The session type structurally satisfies PooledClient (close/aclose); the
    # cast is the validated boundary naming the curl_cffi kind.
    build=cast(PooledClientBuilder, _build_cffi_session),
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


def get_cffi_session() -> CffiSession:
    """Return the shared curl_cffi session bound to the current event loop: reused
    while the loop is unchanged, rebuilt when it changes, and a failed build raises
    (curl_cffi is declared).
    """
    return cast(CffiSession, _CFFI_SESSION_SINGLETON.get())


async def close_cffi_session() -> None:
    """Close the shared curl_cffi session on service shutdown / test reset."""
    await _CFFI_SESSION_SINGLETON.shutdown()


def own_session() -> CffiSession | None:
    """The pooled session, for the shared pool's process-state decisions."""
    return _DEFAULT_CFFI_SESSION


def owns_process_state() -> bool:
    """Whether a cffi session is currently held process-wide."""
    return _DEFAULT_CFFI_SESSION is not None


def close_queued_session() -> None:
    """Release the pooled session, closing it only when no loop owns it."""
    discard_if_unowned_loop(close_cffi_session, "_DEFAULT_CFFI_SESSION", globals())


def session_singleton() -> LoopBoundPooledClient:
    """The pooled descriptor, for builder substitution at the shared seam."""
    return _CFFI_SESSION_SINGLETON
