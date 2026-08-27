"""Memory-adaptive admission control (#self-tuning).

The service senses its own ceiling (cgroup limit when containerized, total RAM
otherwise) and current usage, and throttles crawl-batch dispatch so aggregate
memory rides at a configured target (~92%) instead of climbing into the OOM
killer. No operator tuning required: under target the service runs at full
speed; approaching it, throughput scales down smoothly; at the hard ceiling new
batches pause until usage drains.
"""

from __future__ import annotations

from app.cgroup_sense import _read_int

import asyncio
import os
import time
from dataclasses import dataclass

# Self-managed memory admission targets (no operator knob): the gate rides
# at TARGET and pauses at HARD against the cgroup memory limit it discovers
# itself. Removing the external override lets the app manage its own ceiling.
# Lowered from 0.92/0.97 to prevent first-batch burst overshoot (was spiking
# to ~0.96 of 3GB). Target 0.85 leaves ~0.45 GiB headroom; hard 0.92 still
# provides ~0.24 GiB safety margin before OOM.
_DEFAULT_TARGET = 0.85
_DEFAULT_HARD = 0.92


@dataclass(frozen=True)
class MemoryStats:
    total_bytes: int
    used_bytes: int


def _total_system_ram() -> int:
    """Total physical RAM (host fallback when no cgroup limit applies)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 0


def read_memory_stats(cgroup_root: str = "/sys/fs/cgroup") -> MemoryStats:
    """Current (limit, usage) in bytes; cgroup v2 -> v1 -> host-RAM fallbacks."""
    total_ram = _total_system_ram()

    # cgroup v2
    v2_max = _read_int(os.path.join(cgroup_root, "memory.max"))
    v2_cur = _read_int(os.path.join(cgroup_root, "memory.current"))
    if v2_max is not None and v2_cur is not None:
        return MemoryStats(total_bytes=v2_max, used_bytes=v2_cur)

    # cgroup v1
    v1_max = _read_int(os.path.join(cgroup_root, "memory", "memory.limit_in_bytes"))
    v1_cur = _read_int(os.path.join(cgroup_root, "memory", "memory.usage_in_bytes"))
    if v1_max is not None and v1_cur is not None and 0 < v1_max < total_ram:
        return MemoryStats(total_bytes=v1_max, used_bytes=v1_cur)

    return MemoryStats(total_bytes=total_ram or 0, used_bytes=0)


def batch_scale(used: float, total: float, target: float, hard: float | None = None) -> float:
    """Dispatch multiplier for current pressure.

    <= target -> 1.0 (full speed); target..hard -> linear down to 0;
    >= hard   -> 0 (caller must pause until usage drains).
    """
    if hard is None:
        hard = min(0.999, target + 0.05)
    if total <= 0:
        return 1.0
    ratio = used / total
    if ratio <= target:
        return 1.0
    if ratio >= hard:
        return 0.0
    span = hard - target
    return max(0.0, 1.0 - (ratio - target) / span)



class ResourceGauge:
    """Base admission gauge for one bounded resource.

    A gauge senses a single resource's pressure as a fraction of its ceiling
    via :meth:`_usage_ratio` (subclass-defined) and turns that one signal into
    both the batch-scale factor and the saturation wait. Centralizing the
    derived logic here guarantees the two never drift apart -- the exact bug
    class that previously let a gate's ``batch_scale_factor`` bypass
    ``_usage_ratio``.
    """

    def __init__(
        self,
        target: float | None,
        *,
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        cgroup_root: str = "/sys/fs/cgroup",
    ):
        self.target = target
        self.poll_seconds = poll_seconds
        self.max_wait_seconds = max_wait_seconds
        self.cgroup_root = cgroup_root

    def _usage_ratio(self) -> float:
        """Current pressure as a fraction of the resource ceiling.

        Subclasses override this. Return 0.0 when there is no measured ceiling
        to defend (e.g. an unbounded cgroup) so the gate stays open.
        """
        raise NotImplementedError

    @staticmethod
    def _hard(target: float) -> float:
        return min(0.999, target + 0.05)

    def batch_scale_factor(self) -> float:
        # Both wait_while_saturated and scale read the same _usage_ratio signal.
        return batch_scale(self._usage_ratio(), 1.0, self.target)

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        """Block while at/above the hard ceiling; release below it."""
        budget = self.max_wait_seconds if max_wait_seconds is None else max_wait_seconds
        deadline = time.monotonic() + budget
        hard = self._hard(self.target)
        while self._usage_ratio() >= hard:
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(self.poll_seconds)

    def headroom_fraction(self) -> float:
        """Fraction of the resource ceiling still available before hard limit.

        Returns 1.0 when unbounded (no ceiling to defend). At the hard ceiling
        returns 0.0. Between target and hard, returns the linear fraction
        remaining. Below target returns 1.0 (full headroom).
        """
        usage = self._usage_ratio()
        hard = self._hard(self.target)
        if usage >= hard:
            return 0.0
        if usage <= self.target:
            return 1.0
        return max(0.0, 1.0 - (usage - self.target) / (hard - self.target))


class MemoryGate(ResourceGauge):
    """Global admission gate consulted before each crawl batch dispatch."""

    def __init__(
        self,
        target: float | None = None,
        *,
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        cgroup_root: str = "/sys/fs/cgroup",
    ):
        super().__init__(
            target=(_DEFAULT_TARGET if target is None else target),
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
        )

    def _usage_ratio(self) -> float:
        stats = read_memory_stats(cgroup_root=self.cgroup_root)
        if stats.total_bytes <= 0:
            return 0.0
        return stats.used_bytes / stats.total_bytes
