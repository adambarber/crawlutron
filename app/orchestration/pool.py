"""One pool protocol for the worker-rental lifecycle BrowserPool and DocExtractionPool share.

Both check workers out (``acquire``) and return them (``release``) over a bounded slot set. The
concurrency modality (asyncio vs threads) and lifecycle terminality (recycle vs terminal) stay on each class.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

_T = TypeVar("_T")

__all__ = ["Pool"]


@runtime_checkable
class Pool(Protocol[_T]):
    """A bounded set of ``_T`` workers, checked out and returned."""

    def acquire(self, timeout: float) -> _T | None: ...

    def release(self, worker: _T) -> None: ...

    def __len__(self) -> int: ...
