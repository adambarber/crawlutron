"""The two time/event seams the kernel declares and the seam injects.

No ``time.monotonic`` and no ``asyncio.sleep``/``asyncio.Event`` ever appear in this
package (source-scan gate); the concrete clock and signal live outside ``admission/``.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, runtime_checkable

from admission.values import Instant

__all__ = ["Clock", "Signal"]


@runtime_checkable
class Clock(Protocol):
    """The monotonic time source; the kernel's only timeline."""

    def now(self) -> Instant:
        """The current monotonic instant."""
        ...


@runtime_checkable
class Signal(Protocol):
    """An event-with-optional-timeout wait primitive, provided by the seam."""

    def wait(self, timeout: float | None = None) -> Awaitable[bool]:
        """Suspend until set or until ``timeout`` seconds elapse; True if set.

        The gate awaits this inside :func:`asyncio.wait_for` so the park stays
        bounded by the stale bound even if the seam ignores the timeout."""
        ...

    def set(self) -> None:
        """Wake every waiter."""
        ...

    def clear(self) -> None:
        """Reset so a later ``wait`` does not inherit a set state.

        Edge-triggered signals have no level latch to clear: no-op here.
        Level-latched ones clear so a waiter after a set genuinely suspends."""
        ...
