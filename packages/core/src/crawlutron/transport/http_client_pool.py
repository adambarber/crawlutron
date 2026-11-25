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
    close_cffi_session,
    get_cffi_session,
)
from crawlutron.transport.pooled_clients import (
    LoopBoundPooledClient,
    PooledClientBuilder,
    close_async_http_client,
    get_async_http_client,
)
from crawlutron.vocabulary.strategies import CFFI_IMPERSONATE_TARGET

if TYPE_CHECKING:
    pass


@contextmanager
def pooled_client_builders(
    *,
    http: PooledClientBuilder | None = None,
    cffi: PooledClientBuilder | None = None,
) -> Generator[None]:
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
    "close_async_http_client",
    "close_cffi_session",
    "get_async_http_client",
    "get_cffi_session",
    "pooled_client_builders",
]
