"""Event-loop lag probe and cgroup CPU readings."""

from __future__ import annotations

import asyncio
import os
import time

from app.config.env import parse_bool_env
from app.runtime.cgroup_sense import read_int, read_text

# Self-managed, no operator knob (mirrors app/runtime/memory_pressure.py): the gate
# rides at TARGET and pauses at HARD against the cgroup quota it discovers.
DEFAULT_CPU_TARGET = 0.90


DEFAULT_CPU_HARD = 0.95


# Manual CPU budget override removed; the discovered quota is the ceiling.
DEFAULT_CPU_BUDGET = None


# Opt-in event-loop lag probe feeding admission; off by default
# (CRAWLUTRON_LOOP_LAG_PROBE=1) so tests and minimal deployments pay no task cost.
LOOP_LAG_PROBE_ENABLED = parse_bool_env("CRAWLUTRON_LOOP_LAG_PROBE", default=False)


_LOOP_LAG_SAMPLE_INTERVAL = 0.05


_LOOP_LAG_OK_THRESHOLD = 0.05  # below this lag (s) -> full speed


_LOOP_LAG_HARD_THRESHOLD = 0.30  # at/above this lag (s) -> full throttle


_LOOP_LAG_SMOOTHING = 0.25


class LoopLagProbe:
    """Best-effort event-loop lag sensor.

    A lightweight background tick measures how far the loop falls behind its
    scheduled cadence. A saturated loop (many concurrent coroutines, blocking
    callbacks) lags, an independent pressure signal from CPU/memory: the cgroup
    quota can look fine while the single event loop is the bottleneck. The gate
    multiplies its scale by :meth:`factor` so lag alone can shed load.
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
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin sampling on the current running loop (idempotent)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. import time): never implicitly create
            # one; the owner starts the probe explicitly once a loop runs.
            return
        if self._task is not None and not self._task.done():
            return
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


# --- CPU sensing (cgroup v2 -> v1 -> unbounded) ---


def read_cpu_quota(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """Allocated CPUs for this cgroup (quota / period).

    Returns None when the CPU controller is unlimited or unavailable, so callers
    know there is no CPU ceiling to enforce against.
    """
    # cgroup v2: "quota period", e.g. "200000 100000" -> 2.0 CPUs; "max 100000" -> unlimited.
    v2 = read_text(os.path.join(cgroup_root, "cpu.max"))
    if v2 is not None:
        parts = v2.split()
        if len(parts) >= 2:
            quota_s, period_s = parts[0], parts[1]
            if quota_s == "max":
                return None
            try:
                quota_v2, period_v2 = int(quota_s), int(period_s)
            except ValueError:
                return None
            if period_v2 > 0:
                return quota_v2 / period_v2
        return None

    # Cgroup v1 quota/period; quota -1 means unlimited. Reads are ``int | None``
    # (files may be missing on a v2-only host), so the v2 branch binds its own names.
    quota = read_int(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us"))
    period = read_int(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us"))
    if quota is None or quota < 0 or period is None or period <= 0:
        return None
    return quota / period


def read_cpu_usage_seconds(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """Cumulative CPU time (seconds) consumed by this cgroup.

    v2: ``cpu.stat`` ``usage_usec``; v1: ``cpuacct.usage`` (ns). None if absent.
    """
    # cgroup v2
    stat = read_text(os.path.join(cgroup_root, "cpu.stat"))
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

    # Cgroup v1
    usage_ns = read_int(os.path.join(cgroup_root, "cpuacct", "cpuacct.usage"))
    if usage_ns is None:
        usage_ns = read_int(os.path.join(cgroup_root, "cpu", "cpuacct.usage"))
    if usage_ns is not None:
        return usage_ns / 1_000_000_000.0
    return None
