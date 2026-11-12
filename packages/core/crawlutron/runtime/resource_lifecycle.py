"""The resource lifecycle: one declared release path, one deferred-close owner.

The owner below is the ONE scheduler for "close this later, on the running
loop": a strong ref so asyncio cannot GC the task, failure logged not silent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from crawlutron.runtime import process_state

__all__ = [
    "DeferredClose",
    "ResourceLifecycle",
    "clear_declined_closes",
    "declined_closes",
    "record_declined_close",
    "clear_mapped_value_errors",
    "mapped_value_errors",
    "record_mapped_value_error",
]

_logger = logging.getLogger(__name__)

#: One drain gather's budget: a wedged aclose must not hang shutdown forever.
_DRAIN_BUDGET_SECONDS = 10.0

#: Declined close attempts, ``(reason, resource_kind)`` -> count. A decline is
#: a handled state, not a warning: tests assert on this record (#643).
_DECLINED_CLOSES: dict[tuple[str, str], int] = {}


def record_declined_close(reason: str, resource_kind: str) -> None:
    """Count one declined schedule attempt; the record replaces the printed warning."""
    key = (reason, resource_kind)
    _DECLINED_CLOSES[key] = _DECLINED_CLOSES.get(key, 0) + 1


def declined_closes() -> dict[tuple[str, str], int]:
    """The decline record, for tests asserting a close was declined and why."""
    return dict(_DECLINED_CLOSES)


def clear_declined_closes() -> None:
    """The record's reset door: tests and the reset family empty it between cases."""
    _DECLINED_CLOSES.clear()


#: Mapped foreign ValueErrors, ``(foreign_message,)`` -> count: the bounded
#: record of constructor-belt remaps, so a bug is not silently 422.
_MAPPED_VALUE_ERRORS: dict[tuple[str], int] = {}


def record_mapped_value_error(foreign_message: str) -> None:
    """Count one foreign-ValueError mapping; structured fields only, never a sink."""
    key = (foreign_message,)
    _MAPPED_VALUE_ERRORS[key] = _MAPPED_VALUE_ERRORS.get(key, 0) + 1


def mapped_value_errors() -> tuple[tuple[str, ...], ...]:
    """The mapping record, for tests asserting a foreign error was mapped."""
    return tuple(sorted(_MAPPED_VALUE_ERRORS))


def clear_mapped_value_errors() -> None:
    """The record's reset door: tests and the reset family empty it between cases."""
    _MAPPED_VALUE_ERRORS.clear()


@runtime_checkable
class ResourceLifecycle(Protocol):
    """A resource the service holds that a caller must be able to release."""

    def close(self) -> Awaitable[None] | None:
        """Release the held resource. Idempotent: a second call releases nothing."""
        ...


class DeferredClose[T]:
    """Owns deferred closes for one resource family: schedule, drain, forget.

    ``is_closed`` is required -- only the owner knows how a kind reports closed,
    so the generic owner never guesses. A drain joins closes scheduled mid-drain.
    """

    def __init__(
        self,
        *,
        aclose: Callable[[T], Awaitable[None]],
        is_closed: Callable[[T], bool] = lambda resource: False,
        log: logging.Logger = _logger,
        drain_budget_seconds: float = _DRAIN_BUDGET_SECONDS,
    ) -> None:
        self._aclose = aclose
        self._is_closed = is_closed
        self._log = log
        self._drain_budget_seconds = drain_budget_seconds
        self._pending: set[asyncio.Task[None]] = set()

    def schedule(self, resource: T, *, skip_if_closed: bool = True) -> None:
        """Schedule ``aclose(resource)`` on the running loop; no loop, or a loop
        mid-shutdown, declines honestly -- the caller's own teardown reclaims.
        Declines are RECORDED, not printed (#643 warning-zero).
        """
        if skip_if_closed and self._is_closed(resource):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            record_declined_close("no_loop", type(resource).__name__)
            return
        if loop.is_closed():
            record_declined_close("loop_closed", type(resource).__name__)
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
        """Await every pending close, incl. ones scheduled mid-drain.

        Each gather re-snapshots ``_pending``; the ``sleep(0)`` yield keeps the
        done-callbacks from starving. One round is bounded, fail-closed on timeout.
        """
        while self._pending:
            pending = len(self._pending)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._pending), return_exceptions=True),
                    timeout=self._drain_budget_seconds,
                )
            except TimeoutError:
                self._log.warning(
                    "deferred_close_drain_timed_out",
                    extra={"pending": pending, "owner": type(self).__name__},
                )
                return
            await asyncio.sleep(0)


def _reset_declines() -> None:
    """The registry reset: empty the decline record."""
    _DECLINED_CLOSES.clear()


def _has_declines() -> bool:
    """The registry probe: whether any decline is still recorded."""
    return bool(_DECLINED_CLOSES)


process_state.register(__name__, _reset_declines, _has_declines)
