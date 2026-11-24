"""Pure task-lifetime door: spawn registers real asyncio tasks under a live lease.

Stop cancels and awaits a handle, releasing its admission; scope exit stops all, then the lease.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from crawlutron_admit.values import Count, Duration, Granted

__all__ = ["Grace", "Handle", "NamedRegistry", "RefusalReason", "ScopeRefused", "TaskScope"]

_MAX_GRACE = 60.0


class RefusalReason(enum.StrEnum):
    """The closed reason vocabulary a scope refusal surfaces."""

    TARGET_NOT_REGISTERED = "target_not_registered"
    RESOURCE_PRESSURE = "resource_pressure"


class ScopeRefused(Exception):
    """A non-empty typed refusal: reason, limit, and a bounded retry-after."""

    def __init__(self, *, reason: RefusalReason, limit: Count, retry_after: Duration) -> None:
        super().__init__(f"admission refused: {reason.value}")
        self.reason = reason
        self.limit = limit
        self.retry_after = retry_after


class Grace:
    """A bounded stop wait in seconds: 0 <= value <= _MAX_GRACE, finite."""

    __slots__ = ("_duration",)

    def __init__(self, value: float) -> None:
        duration = Duration(value)
        if duration.value > _MAX_GRACE:
            raise ValueError(f"Grace must be <= {_MAX_GRACE}, got {duration.value!r}")
        self._duration = duration

    @property
    def value(self) -> float:
        return self._duration.value


@dataclass(frozen=True, slots=True)
class Handle:
    """The typed identity of a task registered under a scope's lease."""

    id: str
    task: asyncio.Task[Any]


class NamedRegistry:
    """A pure, named admission registry with its own limit and retry hint."""

    __slots__ = ("_name", "_limit", "_retry_after", "_active")

    def __init__(self, name: str, limit: Count, retry_after: Duration) -> None:
        if not name:
            raise ValueError("NamedRegistry requires a non-empty name")
        self._name = name
        self._limit = limit
        self._retry_after = retry_after
        self._active = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def limit(self) -> Count:
        return self._limit

    @property
    def retry_after(self) -> Duration:
        return self._retry_after

    @property
    def active(self) -> Count:
        return Count(self._active)

    def admit(self) -> None:
        """Consume one slot or refuse with reason, limit and retry-after."""
        if self._active >= self._limit.value:
            raise ScopeRefused(
                reason=RefusalReason.RESOURCE_PRESSURE,
                limit=self._limit,
                retry_after=self._retry_after,
            )
        self._active += 1

    def release(self) -> None:
        """Return one slot, refusing to underflow the registry."""
        if self._active <= 0:
            raise ValueError("NamedRegistry.release underflow: no active slot")
        self._active -= 1


class TaskScope:
    """Two verbs over a live grant: spawn registers a task, stop closes it."""

    __slots__ = ("_granted", "_registry", "_grace", "_handles", "_stopped", "_counter")

    def __init__(self, *, granted: Granted, registry: NamedRegistry, grace: Grace) -> None:
        self._granted = granted
        self._registry = registry
        self._grace = grace
        self._handles: dict[str, Handle] = {}
        self._stopped: set[str] = set()
        self._counter = 0

    @property
    def registry(self) -> NamedRegistry:
        return self._registry

    async def spawn(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> Handle:
        """Admit, then register a real asyncio task built from the factory."""
        self._registry.admit()
        try:
            task = asyncio.create_task(factory())
        except BaseException:
            self._registry.release()
            raise
        self._counter += 1
        handle = Handle(id=f"handle-{self._counter}", task=task)
        self._handles[handle.id] = handle
        return handle

    async def stop(self, handle: Handle, grace: Grace) -> None:
        """Cancel and await a registered task, then release its slot."""
        if handle.id not in self._handles:
            if handle.id in self._stopped:
                return
            raise ScopeRefused(
                reason=RefusalReason.TARGET_NOT_REGISTERED,
                limit=self._registry.limit,
                retry_after=self._registry.retry_after,
            )
        del self._handles[handle.id]
        self._stopped.add(handle.id)
        try:
            handle.task.cancel()
            _done, pending = await asyncio.wait({handle.task}, timeout=grace.value)
            if pending:
                raise ScopeRefused(
                    reason=RefusalReason.RESOURCE_PRESSURE,
                    limit=self._registry.limit,
                    retry_after=Duration(grace.value),
                )
            try:
                handle.task.result()
            except asyncio.CancelledError:
                pass
        finally:
            self._registry.release()

    async def __aenter__(self) -> TaskScope:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop every registered task, then release the lease."""
        refused: ScopeRefused | None = None
        try:
            for handle in list(self._handles.values()):
                try:
                    await self.stop(handle, self._grace)
                except ScopeRefused as failure:
                    if refused is None:
                        refused = failure
        finally:
            self._granted.lease.release()
        if refused is not None:
            raise refused
