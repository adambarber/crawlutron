"""The pooled HTTP and curl_cffi client lifecycle, as one public face.

Construction, loop binding, supersession, bounded close queues and teardown live
in ``crawlutron.transport.pooled_clients``; this re-exports them so callers depend on a role.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from crawlutron.transport import pooled_cffi as _leaf
from crawlutron.transport import pooled_clients as _core
from crawlutron.transport.pooled_cffi import (
    build_pinned_cffi_session,
    close_cffi_session,
    close_queued_session,
    get_cffi_session,
)
from crawlutron.transport.pooled_clients import (
    LoopBoundPooledClient,
    PooledClientBuilder,
    close_async_http_client,
    get_async_http_client,
    has_process_state,
)
from crawlutron.transport.pooled_clients import (
    reset_process_state as _reset_http_pool_state,
)
from crawlutron.vocabulary.strategies import CFFI_IMPERSONATE_TARGET

if TYPE_CHECKING:
    pass


@contextmanager
def _swap_cffi_builder(builder: PooledClientBuilder) -> Generator[None, None, None]:
    """Swap the cffi leaf's builder; restore it on the way out."""
    singleton = _leaf.session_singleton()
    original = singleton.build
    singleton.build = builder
    try:
        yield
    finally:
        singleton.build = original


def reset_process_state() -> None:
    """Release every pooled client, across both pool modules."""
    close_queued_session()
    _reset_http_pool_state()


@contextmanager
def pooled_client_builders(
    *,
    http: PooledClientBuilder | None = None,
    cffi: PooledClientBuilder | None = None,
) -> Generator[None, None, None]:
    """Swap a pooled client's builder for the duration of a block."""
    swaps: list[tuple[LoopBoundPooledClient, PooledClientBuilder]] = []
    if http is not None:
        swaps.append((_core.http_singleton(), http))
    if cffi is not None:
        swaps.append((_leaf.session_singleton(), cffi))
    with _core.swap_pooled_builders(swaps) if swaps else contextlib.nullcontext():
        yield


__all__ = [
    "CFFI_IMPERSONATE_TARGET",
    "build_pinned_cffi_session",
    "close_async_http_client",
    "close_cffi_session",
    "get_async_http_client",
    "get_cffi_session",
    "has_process_state",
    "pooled_client_builders",
    "reset_process_state",
]
