"""The shared process-meter skeleton (#668 extraction): one laddered ledger.

Every process-meter holder (#621) carries the same triple -- a monotonic clock,
a lock-guarded keyed ledger, and the reset/has registration doors.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

__all__ = ["MeterLedger", "register_shared_meter"]


def register_shared_meter[BucketT](owner: str, instance: MeterLedger[BucketT]) -> None:
    """Register a meter's reset/has doors with the process-state registry (#621)."""
    from crawlutron.runtime import process_state

    process_state.register(owner, instance._reset, instance._has)


class MeterLedger[BucketT]:
    """The lock-guarded keyed ledger every process meter composes."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: OrderedDict[str, BucketT] = OrderedDict()
        self._lock = threading.Lock()

    def _reset(self) -> None:
        self._buckets.clear()

    def _has(self) -> bool:
        return bool(self._buckets)

    def residence(self) -> int:
        """Live bucket count: the bounded-ledger probe door (#621 walk)."""
        with self._lock:
            return len(self._buckets)
