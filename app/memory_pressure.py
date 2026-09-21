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

from app.cgroup_sense import _read_int

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
        target: float,
        *,
        hard: float | None = None,
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        cgroup_root: str = "/sys/fs/cgroup",
    ):
        # ``target`` is a plain float here, not optional: the gauge arithmetic
        # below (ratio - target, hard - target) has no defined value without one,
        # and ``ResourceGate()`` used to reach this constructor with None and
        # raise TypeError inside ``_hard``. Callers that want the self-managed
        # default resolve it in their own constructor (see ``MemoryGate`` /
        # ``CpuGate``) before delegating, which is where the optional really
        # belongs.
        self.target = target
        self.hard = self._hard(target) if hard is None else hard
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
        return batch_scale(self._usage_ratio(), 1.0, self.target, hard=self.hard)

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        """Block while at/above the hard ceiling; release below it."""
        budget = self.max_wait_seconds if max_wait_seconds is None else max_wait_seconds
        deadline = time.monotonic() + budget
        while self._usage_ratio() >= self.hard:
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
        if usage >= self.hard:
            return 0.0
        if usage <= self.target:
            return 1.0
        return max(0.0, 1.0 - (usage - self.target) / (self.hard - self.target))


class MemoryGate(ResourceGauge):
    """Global admission gate consulted before each crawl batch dispatch.

    Beyond the trailing ``memory.current`` fraction, the gate also tracks the
    *rate of change* of usage (adversarial perf #2.3: ``memory.current`` is a
    trailing signal). When usage is climbing fast it anticipates the imminent
    pressure and throttles slightly early, so a sudden allocation spike cannot
    blow past the hard ceiling between samples. The raw fraction still drives
    the hard-ceiling wait; only the dispatch scale uses the leading estimate.
    """

    # How much of a fast climb to fold into the leading estimate (capped).
    _MAX_ANTICIPATION = 0.15

    def __init__(
        self,
        target: float | None = None,
        *,
        hard: float | None = None,
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        cgroup_root: str = "/sys/fs/cgroup",
    ):
        use_default_target = target is None
        target = _DEFAULT_TARGET if target is None else target
        if hard is None:
            # Only the self-managed default uses the explicit _DEFAULT_HARD; an
            # explicit target keeps the derived target+0.05 behavior.
            hard = _DEFAULT_HARD if use_default_target else None
        super().__init__(
            target=target,
            hard=hard,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
        )
        self._prev_ratio: float | None = None
        self._prev_time: float = 0.0
        self._slope: float = 0.0

    def _usage_ratio(self) -> float:
        """Current trailing fraction. Pure read (L7): never mutates slope
        state, so interleaved readiness/metrics/wait readers cannot perturb
        the dispatch estimator's slope series."""
        stats = read_memory_stats(cgroup_root=self.cgroup_root)
        if stats.total_bytes <= 0:
            return 0.0
        return stats.used_bytes / stats.total_bytes

    def _track_slope(self, ratio: float) -> None:
        """Advance the climb-rate series. Only the leading estimate calls
        this, so the series reflects consecutive estimator samples."""
        now = time.monotonic()
        if self._prev_ratio is not None:
            dt = now - self._prev_time
            if dt > 0:
                # per-second change; clamp so a single noisy sample cannot spike it.
                self._slope = max(-1.0, min(1.0, (ratio - self._prev_ratio) / dt))
        else:
            self._slope = 0.0
        self._prev_ratio = ratio
        self._prev_time = now

    def leading_usage_ratio(self) -> float:
        """Trailing fraction plus a capped anticipation of the climb rate.

        Only used by the dispatch scale factor, never by the hard-ceiling wait.
        """
        raw = self._usage_ratio()
        self._track_slope(raw)
        if raw >= self.hard:
            return raw
        # If usage is rising, add up to _MAX_ANTICIPATION so we throttle early.
        anticipation = max(0.0, self._slope) * 0.5  # half-life-ish damping
        return min(self.hard, raw + min(self._MAX_ANTICIPATION, anticipation))

    def batch_scale_factor(self) -> float:
        # Use the leading estimate (trailing + climb anticipation) so a fast
        # memory climb throttles dispatch before it reaches the hard ceiling.
        return batch_scale(self.leading_usage_ratio(), 1.0, self.target, hard=self.hard)
