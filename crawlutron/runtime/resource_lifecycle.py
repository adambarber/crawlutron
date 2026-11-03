"""The resource lifecycle: one declared release path, one deferred-close owner.

The owner below is the ONE scheduler for "close this later, on the running
loop": a strong ref so asyncio cannot GC the task, eviction on completion,
failure logged not silent (#589 -- was hand-rolled once per pool, and the
copies had drifted). Each owning module keeps its own closed predicate; the
owner keeps the mechanism.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = ["DeferredClose", "ResourceLifecycle"]

_logger = logging.getLogger(__name__)


@runtime_checkable
class ResourceLifecycle(Protocol):
    """A resource the service holds that a caller must be able to release."""

    def close(self) -> Awaitable[None] | None:
        """Release the held resource. Idempotent: a second call releases nothing."""
        ...


class DeferredClose[T]:
    """Owns deferred closes for one resource family: schedule, drain, forget.

    ``is_closed`` is required -- only the owner knows how a kind reports closed
    (curl_cffi publishes no flag), so the generic owner never guesses. A drain
    re-checks after each gather: a close scheduled while draining is joined
    too, not abandoned to loop teardown (the snapshot gather was).
    """

    def __init__(
        self,
        *,
        aclose: Callable[[T], Awaitable[None]],
        is_closed: Callable[[T], bool] = lambda resource: False,
        log: logging.Logger = _logger,
    ) -> None:
        self._aclose = aclose
        self._is_closed = is_closed
        self._log = log
        self._pending: set[asyncio.Task[None]] = set()

    def schedule(self, resource: T, *, skip_if_closed: bool = True) -> None:
        """Schedule ``aclose(resource)`` on the running loop; no loop, or a loop
        mid-shutdown, declines honestly -- the caller's own teardown reclaims."""
        if skip_if_closed and self._is_closed(resource):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._log.warning("deferred_close_no_loop", extra={"resource": type(resource).__name__})
            return
        if loop.is_closed():
            self._log.warning("deferred_close_loop_closed", extra={"resource": type(resource).__name__})
            return
        task = loop.create_task(self._guarded(resource))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _guarded(self, resource: T) -> None:
        try:
            await self._aclose(resource)
        except Exception as e:
            self._log.warning("deferred_close_failed", extra={"error": str(e)})

    async def drain(self) -> None:
        """Await every pending close, including ones scheduled while draining.

        Each gather re-snapshots ``_pending`` after yielding, so a close
        scheduled mid-drain is joined too (a snapshot gather abandons it).
        The ``sleep(0)`` yield is required: ``asyncio.gather`` completes
        synchronously on an already-done child and would otherwise starve the
        done-callbacks that drop finished tasks out of ``_pending``, spinning
        forever on a closed task.
        """
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
            await asyncio.sleep(0)
