"""The resource lifecycle protocol: one declared release path.

Every class holding a resource speaks this shape -- ``close`` awaited-able or
not, the spelling SpillSink already proved out for the sync/async split.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, runtime_checkable


@runtime_checkable
class ResourceLifecycle(Protocol):
    """A resource the service holds that a caller must be able to release."""

    def close(self) -> Awaitable[None] | None:
        """Release the held resource. Idempotent: a second call releases nothing."""
        ...


__all__ = ["ResourceLifecycle"]
