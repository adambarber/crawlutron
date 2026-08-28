"""Resource-adaptive admission control (#self-tuning): memory + CPU.

Extends the self-tuning admission control from memory-only to every bounded
resource. The service senses its own ceilings (cgroup memory limit, cgroup CPU
quota) and current usage, and throttles crawl-batch dispatch so aggregate usage
rides at a configured target instead of tripping the OOM killer (memory) or
saturating the CPU quota and getting throttled by the kernel (CPU). No operator
tuning required: under target the service runs at full speed; approaching it,
throughput scales down smoothly; at the hard ceiling new batches pause until
usage drains.

CPU is a *rate*, not a level, so :class:`CpuGate` keeps a small history and
reports utilization as (delta CPU-time / delta wall-time) divided by the
allocated CPU quota. That yields a stable fraction-of-budget even though CPU
usage is inherently a sliding window.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from app.cgroup_sense import _read_int, _read_text
from app.memory_pressure import (
    MemoryGate,
    MemoryStats,
    ResourceGauge,
    batch_scale,
    read_memory_stats,
)

# Self-managed CPU admission targets (no operator knob): the gate rides at
# TARGET and pauses at HARD against the cgroup CPU quota it discovers itself.
_DEFAULT_CPU_TARGET = 0.90
_DEFAULT_CPU_HARD = 0.95
# Manual CPU budget override removed: the gate self-derives its ceiling from the
# cgroup CPU quota (or stays open when unbounded). The app manages itself.
_DEFAULT_CPU_BUDGET = None

# Opt-in event-loop lag probe (adversarial perf #2.3: feed loop-lag into
# admission). Disabled by default so test suites and minimal deployments pay no
# background-task cost; enable with BROWSER_CRAWLER_LOOP_LAG_PROBE=1 in the
# container. When disabled the probe reports zero lag and the gate is unaffected.
_LOOP_LAG_PROBE_ENABLED = os.environ.get("BROWSER_CRAWLER_LOOP_LAG_PROBE", "0") == "1"
_LOOP_LAG_SAMPLE_INTERVAL = 0.05
_LOOP_LAG_OK_THRESHOLD = 0.05      # below this lag (s) -> full speed
_LOOP_LAG_HARD_THRESHOLD = 0.30    # at/above this lag (s) -> full throttle
_LOOP_LAG_SMOOTHING = 0.25


class LoopLagProbe:
    """Best-effort event-loop lag sensor.

    Runs a lightweight background tick that measures how far the loop falls
    behind its scheduled cadence. A saturated loop (too many concurrent
    coroutines, blocking callbacks) lags, which is an independent pressure
    signal from CPU/memory: the cgroup CPU quota can look fine while the single
    event loop is the bottleneck. The gate multiplies its scale by
    :meth:`factor` so lag alone can shed load.
    """

    def __init__(
        self,
        *,
        interval: float = _LOOP_LAG_SAMPLE_INTERVAL,
        ok_threshold: float = _LOOP_LAG_OK_THRESHOLD,
        hard_threshold: float = _LOOP_LAG_HARD_THRESHOLD,
        smoothing: float = _LOOP_LAG_SMOOTHING,
    ):
        self._interval = interval
        self._ok = ok_threshold
        self._hard = hard_threshold
        self._smoothing = smoothing
        self._lag = 0.0
        self._task: "asyncio.Task | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None

    def start(self) -> None:
        """Begin sampling on the current running loop (idempotent)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop is None or not loop.is_running():
            return
        if self._task is not None and not self._task.done():
            return
        self._loop = loop
        self._task = loop.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            while True:
                t0 = time.monotonic()
                await asyncio.sleep(self._interval)
                elapsed = time.monotonic() - t0
                lag = max(0.0, elapsed - self._interval)
                self._lag = self._lag * (1.0 - self._smoothing) + lag * self._smoothing
        except asyncio.CancelledError:
            return
        except (RuntimeError, Exception):
            # Loop closed or other failure: stop sampling, report zero lag.
            self._lag = 0.0
            return

    def get(self) -> float:
        return self._lag

    def factor(self) -> float:
        """Scale multiplier from current lag: 1.0 below ``ok``, 0.0 at/above ``hard``."""
        if self._lag <= self._ok:
            return 1.0
        if self._lag >= self._hard:
            return 0.0
        return max(0.0, 1.0 - (self._lag - self._ok) / (self._hard - self._ok))


# ---------------------------------------------------------------------------
# CPU sensing (cgroup v2 -> v1 -> unbounded)
# ---------------------------------------------------------------------------

def read_cpu_quota(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """Allocated CPUs for this cgroup (quota / period).

    Returns None when the CPU controller is unlimited or unavailable, so callers
    know there is no CPU ceiling to enforce against.
    """
    # cgroup v2: "quota period", e.g. "200000 100000" -> 2.0 CPUs; "max 100000" -> unlimited.
    v2 = _read_text(os.path.join(cgroup_root, "cpu.max"))
    if v2 is not None:
        parts = v2.split()
        if len(parts) >= 2:
            quota_s, period_s = parts[0], parts[1]
            if quota_s == "max":
                return None
            try:
                quota, period = int(quota_s), int(period_s)
            except ValueError:
                return None
            if period > 0:
                return quota / period
        return None

    # cgroup v1: cpu.cfs_quota_us / cpu.cfs_period_us; quota -1 means unlimited.
    quota = _read_int(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us"))
    period = _read_int(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us"))
    if quota is not None and period is not None:
        if quota < 0:
            return None
        if period > 0:
            return quota / period
    return None


def read_cpu_usage_seconds(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """Cumulative CPU time (seconds) consumed by this cgroup.

    cgroup v2: ``cpu.stat`` ``usage_usec``. cgroup v1: ``cpuacct.usage`` (ns).
    Returns None when unavailable.
    """
    # cgroup v2
    stat = _read_text(os.path.join(cgroup_root, "cpu.stat"))
    if stat is not None:
        for line in stat.splitlines():
            if line.startswith("usage_usec"):
                parts = line.split()
                if len(parts) == 2:
                    try:
                        return int(parts[1]) / 1_000_000.0
                    except ValueError:
                        return None
        return None

    # cgroup v1
    usage_ns = _read_int(os.path.join(cgroup_root, "cpuacct", "cpuacct.usage"))
    if usage_ns is None:
        usage_ns = _read_int(os.path.join(cgroup_root, "cpu", "cpuacct.usage"))
    if usage_ns is not None:
        return usage_ns / 1_000_000_000.0
    return None


# ---------------------------------------------------------------------------
# CPU admission gate (rate-based)
# ---------------------------------------------------------------------------

class CpuGate(ResourceGauge):
    """Admission gate driven by CPU utilization relative to the cgroup quota.

    Utilization is a *rate*: we keep the last (usage, wall) sample and report
    (delta CPU seconds / delta wall seconds) / quota. Below ``target`` the gate
    returns scale 1.0 (full speed); between target and hard it scales down
    linearly; at/above hard the gate makes dispatchers wait until utilization
    drains below the hard ceiling.

    When the cgroup is CPU-unbounded and no manual ``cpu_budget`` is configured,
    the gate reports utilization as None and never throttles (scale 1.0, no
    wait) -- there is no measured ceiling to defend, so it stays out of the way.
    """

    def __init__(
        self,
        target: float | None = None,
        *,
        hard: float | None = None,
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        cgroup_root: str = "/sys/fs/cgroup",
        sample_seconds: float = 0.25,
        min_window_seconds: float = 0.05,
        cpu_budget: float | None = None,
    ):
        use_default_target = target is None
        target = _DEFAULT_CPU_TARGET if target is None else target
        if hard is None:
            # Only the self-managed default uses the explicit _DEFAULT_CPU_HARD;
            # an explicit target keeps the derived target+0.05 behavior.
            hard = _DEFAULT_CPU_HARD if use_default_target else None
        super().__init__(
            target=target,
            hard=hard,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
        )
        self.sample_seconds = sample_seconds
        self.min_window_seconds = min_window_seconds
        self._cpu_budget = cpu_budget if cpu_budget is not None else _DEFAULT_CPU_BUDGET
        self._last_usage: float | None = None
        self._last_wall: float | None = None
        self._ratio: float = 0.0
        self._quota: float | None = None
        self._quota_loaded: bool = False

    def _quota_cpus(self) -> float | None:
        """Allocated CPUs: the cgroup quota, or a manual budget if unbounded."""
        if not self._quota_loaded:
            self._quota = read_cpu_quota(self.cgroup_root)
            self._quota_loaded = True
        if self._quota is None:
            return self._cpu_budget
        return self._quota

    def _sample(self) -> float | None:
        """Current utilization fraction vs quota, or None if unbounded.

        Updates the rolling sample only when at least ``min_window_seconds`` of
        wall time has elapsed, so back-to-back calls do not produce a noisy rate.
        """
        quota = self._quota_cpus()
        if quota is None or quota <= 0:
            return None
        usage = read_cpu_usage_seconds(self.cgroup_root)
        now = time.monotonic()
        if usage is None:
            return None
        if self._last_usage is None or self._last_wall is None:
            self._last_usage = usage
            self._last_wall = now
            return self._ratio  # 0.0 on first sight
        dt = now - self._last_wall
        if dt < self.min_window_seconds:
            return self._ratio
        du = usage - self._last_usage
        instant = (du / dt) / quota if dt > 0 else self._ratio
        # Exponential smoothing absorbs scheduler bursts without erasing signal.
        self._ratio = instant if self._ratio == 0.0 else self._ratio * 0.5 + instant * 0.5
        self._last_usage = usage
        self._last_wall = now
        return self._ratio

    def _usage_ratio(self) -> float:
        r = self._sample()
        return 0.0 if r is None else r

    def cpu_ratio(self) -> float:
        """Latest utilization fraction vs quota (0.0 when unbounded)."""
        return self._usage_ratio()

    def is_warm(self) -> bool:
        """Whether the gate has a real utilization sample yet.

        True when the cgroup is CPU-unbounded (no ceiling to defend, so the full
        concurrency multiplier is always safe) or once at least one rate sample
        has been taken. False only on a cold start with a real CPU ceiling --
        exactly when the first recursive batch should ramp conservatively instead
        of launching ``global_concurrency * multiplier`` parallel fetches, which
        can briefly exceed a tight CPU quota before the gate engages.
        """
        if self._quota_cpus() is None:
            return True
        return self._last_usage is not None and self._last_wall is not None

# ---------------------------------------------------------------------------
# Combined gate over memory + CPU
# ---------------------------------------------------------------------------

class ResourceGate:
    """Combined admission gate over memory + CPU.

    Dispatchers wait while *either* resource is saturated, and the batch-scale
    factor is the *minimum* across resources, so the most-contended resource
    sets the pace. This is the single gate the crawl engine consults before each
    recursive batch dispatch.
    """

    def __init__(
        self,
        *,
        target_mem: float | None = None,
        target_cpu: float | None = None,
        cgroup_root: str = "/sys/fs/cgroup",
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        sample_seconds: float = 0.25,
        cpu_budget: float | None = None,
    ):
        self.memory = MemoryGate(
            target=target_mem,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
        )
        self.cpu = CpuGate(
            target=target_cpu,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
            sample_seconds=sample_seconds,
            cpu_budget=cpu_budget,
        )
        self._poll_seconds = poll_seconds
        # Event-loop lag is an independent throttle signal (adversarial perf
        # #2.3). The probe is opt-in (BROWSER_CRAWLER_LOOP_LAG_PROBE) so tests
        # and minimal deployments do not pay for a background task; when off it
        # reports zero lag and leaves the gate untouched.
        self._lag_probe = LoopLagProbe()
        if _LOOP_LAG_PROBE_ENABLED:
            self._lag_probe.start()

    def memory_ratio(self) -> float:
        return self.memory._usage_ratio()

    def cpu_ratio(self) -> float:
        return self.cpu._usage_ratio()

    def loop_lag(self) -> float:
        """Current measured event-loop lag in seconds (0.0 when probe disabled)."""
        return self._lag_probe.get()

    def close(self) -> None:
        """Stop the optional loop-lag probe (call on shutdown)."""
        self._lag_probe.stop()

    def batch_scale_factor(self) -> float:
        base = min(self.memory.batch_scale_factor(), self.cpu.batch_scale_factor())
        # A lagging event loop is its own bottleneck regardless of cgroup CPU;
        # shed load proportionally when it falls behind its scheduled cadence.
        return base * self._lag_probe.factor()

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        budget = self.memory.max_wait_seconds if max_wait_seconds is None else max_wait_seconds
        deadline = time.monotonic() + budget
        while True:
            mem_r = self.memory._usage_ratio()
            cpu_r = self.cpu._usage_ratio()
            if mem_r < self.memory.hard and cpu_r < self.cpu.hard:
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(self._poll_seconds)

    def http_scale_factor(self) -> float:
        """Scale factor for HTTP-only (browser-free) tasks.

        HTTP fetches add little memory and no browser process. When the CPU has
        ample headroom (> 50%), HTTP work is *not* bound by the memory held by
        browser processes of other domains -- it rides the CPU-implied scale,
        using the spare CPU headroom. Only when CPU is itself the tight resource
        does it fall back to the combined memory+CPU gate.
        """
        cpu_headroom = self.cpu.headroom_fraction()
        if cpu_headroom > 0.5:
            # CPU has ample headroom -> HTTP tasks ride the CPU scale, escaping
            # the browser-held-memory bottleneck that throttles browser batches.
            return self.cpu.batch_scale_factor()
        # CPU is tight -> HTTP tasks respect combined pressure
        return self.batch_scale_factor()
