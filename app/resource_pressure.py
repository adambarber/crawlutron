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
from typing import Any

from app.cgroup_sense import _read_int, _read_text
from app.memory_pressure import (
    MemoryGate,
    ResourceGauge,
    read_memory_stats,
)
from app.validation import get_env_float, get_env_int, parse_bool_env

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
_LOOP_LAG_PROBE_ENABLED = parse_bool_env("BROWSER_CRAWLER_LOOP_LAG_PROBE", default=False)
_LOOP_LAG_SAMPLE_INTERVAL = 0.05
_LOOP_LAG_OK_THRESHOLD = 0.05  # below this lag (s) -> full speed
_LOOP_LAG_HARD_THRESHOLD = 0.30  # at/above this lag (s) -> full throttle
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
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

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
                quota_v2, period_v2 = int(quota_s), int(period_s)
            except ValueError:
                return None
            if period_v2 > 0:
                return quota_v2 / period_v2
        return None

    # cgroup v1: cpu.cfs_quota_us / cpu.cfs_period_us; quota -1 means unlimited.
    # ``quota``/``period`` are ``int | None`` here (the file may be missing on
    # v2-only hosts); the v2 branch above uses its own names so the two reads
    # never share a variable that has to be both.
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

    _default_target = _DEFAULT_CPU_TARGET
    _default_hard = _DEFAULT_CPU_HARD

    def __init__(
        self,
        target: float | None = None,
        *,
        sample_seconds: float = 0.25,
        min_window_seconds: float = 0.05,
        cpu_budget: float | None = None,
        **kwargs: Any,
    ):
        super().__init__(target=target, **kwargs)
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

    def start_lag_probe(self) -> None:
        """Start the loop-lag sampler on the running loop (M16).

        Called from lifespan startup, not __init__: the module-level gate is
        constructed at import time with no running loop, so an __init__-time
        start silently never schedules and lag reads 0.0 forever. Idempotent.
        """
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


# ---------------------------------------------------------------------------
# Two-tier weighted admission (replaces the stop-the-world pause)
# ---------------------------------------------------------------------------
# HTTP crawls are cheap (a few MiB of page + extraction state per in-flight
# task); browser crawls are expensive (dominated by the persistent
# browser-pool processes, which hold most of the cgroup RAM). The old
# dispatcher called ``wait_while_saturated()`` and parked the ENTIRE crawl
# while any resource was saturated -- which, with a fixed browser pool, meant
# the cheap HTTP path was throttled to zero whenever browsers held memory.
#
# :class:`WeightedAdmission` instead grants, per batch, as many HTTP tasks as
# fit the live RAM/CPU headroom, then up to a small browser-tier cap. If nothing
# fits it yields briefly (backpressure) instead of parking the whole crawl, so
# the HTTP path can scale to hundreds of concurrent pages while the browser tier
# stays RAM-budgeted.

_HTTP_TASK_RAM_BYTES = get_env_int("BROWSER_CRAWLER_HTTP_TASK_RAM_BYTES", 48 * 1024 * 1024, lo=0)
# P2-H: the 700 MiB default reservation sits BELOW the measured RSS range for a
# live renderer (~600-900 MiB once WebExtensions/Web Content are counted; see
# app/crawl/strategies/browser_pool.py). That gap is deliberate, not an
# oversight, and this comment exists so it is not "corrected" by reflex: the
# reservation is the weight the weighted admission subtracts from the RAM
# budget per browser task, and raising it toward the top of the measured range
# lowers the number of concurrent browsers the admission will grant. Effective
# browser concurrency is a capacity decision that trades throughput against the
# hard container memory ceiling (docker-compose mem_limit; the failure mode is
# an OOM kill of the whole service), so the number stays conservative while the
# measured range straddles it. The right knob for that trade-off is here
# (BROWSER_CRAWLER_BROWSER_TASK_RAM_BYTES), not a change to the default.
_BROWSER_TASK_RAM_BYTES = get_env_int("BROWSER_CRAWLER_BROWSER_TASK_RAM_BYTES", 700 * 1024 * 1024, lo=0)
# Persistent markitdown workers (app.crawl.extraction._DocWorker): each imports
# markitdown plus the PDF/Office parsers once and stays alive for the process, so
# this is a FIXED reservation, not a per-task estimate. Measured footprint is
# 100-300 MiB apiece depending on which parsers get touched; the conservative
# end is the default because the container ceiling is hard (docker-compose
# mem_limit) and the failure mode is an OOM kill of the whole service.
_DOC_TASK_RAM_BYTES = get_env_int("BROWSER_CRAWLER_DOC_TASK_RAM_BYTES", 300 * 1024 * 1024, lo=0)
# Default persistent markitdown worker count; mirrors the 3 GiB container
# ceiling the compose pins. Overridable via BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS.
_DEFAULT_DOC_WORKERS = 2
_ADMIT_YIELD_SECONDS = get_env_float("BROWSER_CRAWLER_ADMIT_YIELD_SECONDS", 0.05, lo=0.0)
_MAX_HTTP_CONCURRENCY = get_env_int("BROWSER_CRAWLER_MAX_HTTP_CONCURRENCY", 512, lo=1)


# --- Shared RAM-budget sizing (browser pool + httpx pool derive from this) ---
# Both the browser pool and the HTTP connection pool size themselves from the cgroup
# memory limit, centralized here (import-safe from settings) as the single source of
# truth so the connection ceiling tracks the weighted admission's HTTP concurrency.
_BROWSER_RAM_BYTES = get_env_int("BROWSER_CRAWLER_BROWSER_RAM_BYTES", 700 * 1024 * 1024)
_BROWSER_RAM_FRACTION = get_env_float("BROWSER_CRAWLER_BROWSER_RAM_FRACTION", 0.5, lo=0.0, hi=1.0)
_MAX_DERIVED_POOL_SIZE = get_env_int("BROWSER_CRAWLER_MAX_DERIVED_POOL_SIZE", 4)


def doc_worker_count() -> int:
    """Configured persistent markitdown worker count (process pool size).

    The single home for this count: ``app.crawl.extraction`` borrows it so the
    pool it spawns is exactly the size admission reserved. Reading the env var
    in two places would let the spawned pool and the RAM reservation drift
    apart. A malformed value falls back to the default rather than aborting
    startup -- extraction is a nicety, not a load-bearing path.
    """
    try:
        return get_env_int("BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS", _DEFAULT_DOC_WORKERS, lo=1)
    except ValueError:
        return _DEFAULT_DOC_WORKERS


def derive_browser_pool_size(limit_bytes: int | None = None) -> int:
    """Single global browser-pool size from the cgroup memory limit.

    Honors ``BROWSER_CRAWLER_BROWSER_POOL_SIZE``; else derives so the pool uses at
    most ``_BROWSER_RAM_FRACTION`` of RAM; falls back to 1 when no limit is
    discoverable. Shared by ``app.crawl.strategies.browser_pool`` and the httpx
    pool sizing so the connection ceiling tracks the admission's HTTP concurrency.
    """
    explicit = get_env_int("BROWSER_CRAWLER_BROWSER_POOL_SIZE", 0, lo=0)
    if explicit > 0:
        return explicit
    limit = limit_bytes if limit_bytes is not None else read_memory_stats().total_bytes
    if limit and limit > 0:
        budget = int(limit * _BROWSER_RAM_FRACTION)
        size = max(1, budget // _BROWSER_RAM_BYTES)
        return min(size, _MAX_DERIVED_POOL_SIZE)
    return 1


def max_http_concurrency_for(
    limit_bytes: int,
    pool_size: int,
    *,
    http_task_bytes: int = _HTTP_TASK_RAM_BYTES,
    browser_task_bytes: int = _BROWSER_TASK_RAM_BYTES,
    doc_workers: int = 0,
    doc_task_bytes: int = _DOC_TASK_RAM_BYTES,
) -> int:
    """Admission's HTTP concurrency ceiling for a given RAM limit + fixed pools.

    Reserves the browser pool's and the doc pool's fixed RAM, then divides the
    remainder by the per-HTTP-task estimate. This is exactly the ceiling
    ``WeightedAdmission`` uses, so the httpx connection pool can be bound to it
    with zero drift as RAM scales.
    """
    limit = max(limit_bytes, 256 * 1024 * 1024)
    reserve = pool_size * browser_task_bytes + doc_workers * doc_task_bytes
    free = max(0, limit - reserve)
    return max(1, min(_MAX_HTTP_CONCURRENCY, free // max(1, http_task_bytes)))


@dataclass
class AdmissionPolicy:
    """Sizing inputs for :class:`WeightedAdmission` (all in bytes / counts)."""

    http_task_bytes: int
    browser_task_bytes: int
    browser_pool_reserve_bytes: int
    max_http_concurrency: int
    max_browser_concurrency: int
    # The persistent markitdown worker pool is a fixed RAM consumer like the
    # browser pool; its reservation is surfaced separately so a caller can see
    # the budget that is already spent before any task is admitted.
    doc_pool_reserve_bytes: int = 0
    # The fraction of RAM ``derive_browser_pool_size`` assumed the pool may
    # hold. The browser tier must not shrink against its own reserved usage
    # (T9); only surplus usage beyond this fraction yields browser capacity.
    browser_ram_fraction: float = _BROWSER_RAM_FRACTION


def _build_admission_policy(
    limit_bytes: int,
    *,
    pool_size: int,
    max_concurrent_pages: int,
    http_task_bytes: int = _HTTP_TASK_RAM_BYTES,
    browser_task_bytes: int = _BROWSER_TASK_RAM_BYTES,
    doc_workers: int = 0,
    doc_task_bytes: int = _DOC_TASK_RAM_BYTES,
) -> AdmissionPolicy:
    """Derive an admission policy from the cgroup memory limit + fixed pools.

    The browser pool's and the doc pool's fixed RAM is reserved up front; the
    remaining RAM is divided by the per-HTTP-task estimate to get the HTTP
    concurrency ceiling.
    """
    limit = max(limit_bytes, 256 * 1024 * 1024)
    browser_pool_reserve_bytes = pool_size * browser_task_bytes
    doc_pool_reserve_bytes = doc_workers * doc_task_bytes
    max_http = max_http_concurrency_for(
        limit,
        pool_size,
        http_task_bytes=http_task_bytes,
        browser_task_bytes=browser_task_bytes,
        doc_workers=doc_workers,
        doc_task_bytes=doc_task_bytes,
    )
    return AdmissionPolicy(
        http_task_bytes=http_task_bytes,
        browser_task_bytes=browser_task_bytes,
        browser_pool_reserve_bytes=browser_pool_reserve_bytes,
        max_http_concurrency=max_http,
        max_browser_concurrency=max(1, pool_size * max_concurrent_pages),
        doc_pool_reserve_bytes=doc_pool_reserve_bytes,
    )


def default_admission_policy(gate: ResourceGate) -> WeightedAdmission:
    """A conservative policy used when the caller does not supply one.

    Assumes a 2-process browser pool and the default doc worker count (the 3 GiB
    default) so tests / minimal deployments stay safe; production passes a
    policy built from the real pools.
    """
    stats = read_memory_stats()
    limit = stats.total_bytes or (4 * 1024 * 1024 * 1024)
    return WeightedAdmission(
        gate,
        _build_admission_policy(
            limit,
            pool_size=2,
            max_concurrent_pages=3,
            doc_workers=doc_worker_count(),
        ),
    )


class WeightedAdmission:
    """RAM/CPU-weighted, two-tier batch admission.

    HTTP tasks are admitted up to ``max_http_concurrency * headroom``; browser
    tasks up to the (small) browser-tier cap. ``admit`` never blocks for long:
    when nothing fits it yields :data:`_ADMIT_YIELD_SECONDS` of backpressure.
    """

    def __init__(self, gate: ResourceGate, policy: AdmissionPolicy) -> None:
        self._gate = gate
        self._p = policy

    def headroom_fraction(self) -> float:
        return min(1.0 - self._gate.memory_ratio(), 1.0 - self._gate.cpu_ratio())

    def http_capacity(self) -> int:
        return max(0, int(self._p.max_http_concurrency * self.headroom_fraction()))

    def browser_capacity(self) -> int:
        # Browsers hold most of the cgroup RAM, and that footprint is exactly
        # what ``browser_pool_reserve_bytes`` reserved up front — so the tier
        # must not shrink against its own reserved usage (double scaling, T9).
        # Capacity is full while usage stays within the fraction the pool
        # sizing assumed, then ramps down to zero as the box exhausts, and the
        # CPU gate applies on top. Never admit new browser renders when
        # saturated.
        ratio = self._gate.memory_ratio()
        span = max(1e-9, 1.0 - self._p.browser_ram_fraction)
        usable = min(max(0.0, (1.0 - ratio) / span), 1.0 - self._gate.cpu_ratio())
        return max(0, int(self._p.max_browser_concurrency * usable))

    async def admit(self, want_http: int, want_browser: int) -> tuple[int, int]:
        h = min(want_http, self.http_capacity())
        b = min(want_browser, self.browser_capacity())
        if h + b == 0 and (want_http + want_browser) > 0:
            await asyncio.sleep(_ADMIT_YIELD_SECONDS)
        return h, b
