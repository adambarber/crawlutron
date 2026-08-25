"""Memory-adaptive admission control (#self-tuning).

The service senses its own ceiling (cgroup limit when containerized, total RAM
otherwise) and current usage, and throttles crawl-batch dispatch so aggregate
memory rides at a configured target (~92%) instead of climbing into the OOM
killer. No operator tuning required: under target the service runs at full
speed; approaching it, throughput scales down smoothly; at the hard ceiling new
batches pause until usage drains.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

_DEFAULT_TARGET = float(os.environ.get("BROWSER_CRAWLER_MEMORY_TARGET", "0.92"))
_DEFAULT_HARD = float(os.environ.get("BROWSER_CRAWLER_MEMORY_HARD", "0.97"))


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


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            value = fh.read().strip()
    except OSError:
        return None
    if not value or value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


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


class MemoryGate:
    """Global admission gate consulted before each crawl batch dispatch."""

    def __init__(
        self,
        target: float | None = None,
        *,
        poll_seconds: float = 0.1,
        cgroup_root: str = "/sys/fs/cgroup",
    ):
        self.target = _DEFAULT_TARGET if target is None else target
        self.poll_seconds = poll_seconds
        self.cgroup_root = cgroup_root

    def _usage_ratio(self) -> float:
        stats = read_memory_stats(cgroup_root=self.cgroup_root)
        if stats.total_bytes <= 0:
            return 0.0
        return stats.used_bytes / stats.total_bytes

    def batch_scale_factor(self) -> float:
        stats = read_memory_stats(cgroup_root=self.cgroup_root)
        return batch_scale(stats.used_bytes, stats.total_bytes, self.target)

    async def wait_while_saturated(self, max_wait_seconds: float = 30.0) -> None:
        """Block while at/above the hard ceiling; release below it."""
        deadline = time.monotonic() + max_wait_seconds
        while self._usage_ratio() >= min(0.999, self.target + 0.05):
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(self.poll_seconds)
