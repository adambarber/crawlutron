"""The pooled curl_cffi session: one per event loop, superseded through the shared queue.

The canonical singleton lives with its own slot namespace; the shared lifecycle
descriptors and bounded close queue live in :mod:`crawlutron.transport.pooled_clients`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

import curl_cffi.requests as cffi_requests

from crawlutron.runtime import process_state
from crawlutron.transport.egress_proxy_runtime import egress_proxy_url
from crawlutron.transport.pooled_client_release import discard_if_unowned_loop
from crawlutron.transport.pooled_clients import (
    LoopBoundPooledClient,
    PooledClientBuilder,
    bind_to_running_loop,
)
from safe_url.host_policy import HostPolicy

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings

from crawlutron.vocabulary.strategies import CFFI_IMPERSONATE_TARGET

#: Runtime alias: annotations name the session type, and tests import this
#: module directly, so the alias must exist at runtime too.
CffiSession = cffi_requests.AsyncSession

_DEFAULT_CFFI_SESSION: CffiSession | None = None


_CFFI_SESSION_INIT_LOCK = threading.Lock()


def _build_cffi_session(settings: CrawlerSettings | None = None) -> CffiSession:
    """Build a shared proxied curl_cffi AsyncSession; no timeout, so one session
    serves every per-request budget. ``settings`` matches the shared builder seam.
    """
    from crawlutron.config.settings_cache import get_cached_settings

    if settings is None:
        settings = get_cached_settings()
    # discard_cookies=True: no built-in jar (cross-tenant cookie replay).
    # trust_env=False: ambient proxies must never bypass the loopback door.
    session: cffi_requests.AsyncSession = cffi_requests.AsyncSession(
        impersonate=CFFI_IMPERSONATE_TARGET,
        discard_cookies=True,
        proxy=egress_proxy_url(HostPolicy.from_settings(settings), settings=settings),
        trust_env=False,
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


def owns_process_state() -> bool:
    """Whether a cffi session is currently held process-wide."""
    return _DEFAULT_CFFI_SESSION is not None


def close_queued_session(reset: bool = False) -> None:
    """Release the pooled session, closing it only when no loop owns it."""
    discard_if_unowned_loop(close_cffi_session, "_DEFAULT_CFFI_SESSION", globals(), reset=reset)


def session_singleton() -> LoopBoundPooledClient:
    """The pooled descriptor, for builder substitution at the shared seam."""
    return _CFFI_SESSION_SINGLETON


def _reset() -> None:
    """Release the pooled session, closing it only when no loop owns it."""
    close_queued_session(reset=True)


process_state.register(__name__, _reset, owns_process_state)
