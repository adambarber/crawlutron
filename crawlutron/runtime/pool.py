"""Two pool protocols for a worker-rental lifecycle that genuinely splits on concurrency.

BrowserPool awaits a coroutine on acquire (AsyncPool); DocExtractionPool blocks the current thread
(SyncPool). No single flat surface holds both, so each pool conforms to the protocol matching it.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, TypeVar, runtime_checkable

_C = TypeVar("_C", contravariant=True)
_T = TypeVar("_T")

__all__ = ["AsyncPool", "SyncPool"]


class _PoolBase(Protocol[_C]):
    """Return verbs shared by every pool: release a worker and report slot count."""

    def release(self, worker: _C) -> None: ...

    def __len__(self) -> int: ...


@runtime_checkable
class AsyncPool(_PoolBase[_T], Protocol[_T]):
    """A bounded set of ``_T`` workers checked out by awaiting a coroutine."""

    def acquire(self, timeout: float) -> Awaitable[_T | None]: ...


@runtime_checkable
class SyncPool(_PoolBase[_T], Protocol[_T]):
    """A bounded set of ``_T`` workers checked out by blocking the current thread."""

    def acquire(self, timeout: float) -> _T | None: ...
