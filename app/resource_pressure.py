"""Resource-adaptive admission control: every bounded resource, not just memory.

:class:`app.memory_pressure.MemoryGate` senses the memory ceiling and throttles
crawl-batch dispatch against it, with no operator knob. This module extends
that to the other bounded resources the service runs under: the cgroup CPU
quota, which the kernel would otherwise throttle from behind the crawler's back,
and the event loop, whose lag is the first place queueing delay shows up.

CPU is a *rate*, not a level, so :class:`CpuGate` keeps a small history and
reports utilization as (delta CPU-time / delta wall-time) divided by the
allocated CPU quota. That yields a stable fraction-of-budget even though CPU
usage is inherently a sliding window.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.cgroup_sense import _read_int, _read_text
from app.env import get_env_float, get_env_int, parse_bool_env
from app.memory_pressure import (
    MemoryGate,
    ResourceGauge,
    read_memory_stats,
)

# Self-managed, no operator knob (mirrors app/memory_pressure.py): the gate
# rides at TARGET and pauses at HARD against the cgroup quota it discovers.
_DEFAULT_CPU_TARGET = 0.90
_DEFAULT_CPU_HARD = 0.95
# Manual CPU budget override removed; the discovered quota is the ceiling.
_DEFAULT_CPU_BUDGET = None

# Opt-in event-loop lag probe: feed loop-lag into
# admission. Disabled by default so test suites and minimal deployments pay no
# background-task cost; enable with CRAWLUTRON_LOOP_LAG_PROBE=1 in the
# container. When disabled the probe reports zero lag and the gate is unaffected.
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

    # Cgroup v1: cpu.cfs_quota_us / cpu.cfs_period_us; quota -1 means unlimited.
    # Both reads are ``int | None`` (the files may be missing on a v2-only host),
    # which is why the v2 branch above binds its own names.
    quota = _read_int(os.path.join(cgroup_root, "cpu", "cpu.cfs_quota_us"))
    period = _read_int(os.path.join(cgroup_root, "cpu", "cpu.cfs_period_us"))
    if quota is None or quota < 0 or period is None or period <= 0:
        return None
    return quota / period


def read_cpu_usage_seconds(cgroup_root: str = "/sys/fs/cgroup") -> float | None:
    """Cumulative CPU time (seconds) consumed by this cgroup.

    v2: ``cpu.stat`` ``usage_usec``; v1: ``cpuacct.usage`` (ns). None if absent.
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

    # Cgroup v1
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
    returns scale 1.0; between target and hard it scales down linearly; at or
    above hard dispatchers wait until utilization drains below the ceiling.
    When the cgroup is CPU-unbounded and no ``cpu_budget`` is configured the
    gate reports None and never throttles: no measured ceiling, no defence.
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

    def _read_usage(self) -> float:
        r = self._sample()
        return 0.0 if r is None else r

    def cpu_ratio(self) -> float:
        """Latest utilization fraction vs quota (0.0 when unbounded)."""
        return self.usage_ratio()

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
        clock: Callable[[], float] = time.monotonic,
        memory_ratio: Callable[[], float] | None = None,
        cpu_ratio: Callable[[], float] | None = None,
        lag_probe_enabled: bool = LOOP_LAG_PROBE_ENABLED,
    ):
        self.memory = MemoryGate(
            target=target_mem,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
            clock=clock,
            usage_ratio=memory_ratio,
        )
        self.cpu = CpuGate(
            target=target_cpu,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
            sample_seconds=sample_seconds,
            cpu_budget=cpu_budget,
            usage_ratio=cpu_ratio,
        )
        self._poll_seconds = poll_seconds
        # Event-loop lag is an independent throttle signal.
        # The probe is opt-in (CRAWLUTRON_LOOP_LAG_PROBE) so tests
        # and minimal deployments do not pay for a background task; when off it
        # reports zero lag and leaves the gate untouched. Injected rather than
        # read from the module global so a test can enable it without rebinding
        # a name the rest of the tree also reads.
        self._lag_probe = LoopLagProbe()
        self._lag_probe_enabled = lag_probe_enabled

    def start_lag_probe(self) -> None:
        """Start the loop-lag sampler on the running loop.

        Called from lifespan startup, not __init__: the module-level gate is
        constructed at import time with no running loop, so an __init__-time
        start silently never schedules and lag reads 0.0 forever. Idempotent.
        """
        if self._lag_probe_enabled:
            self._lag_probe.start()

    def memory_ratio(self) -> float:
        return self.memory.usage_ratio()

    def cpu_ratio(self) -> float:
        return self.cpu.usage_ratio()

    def close(self) -> None:
        """Stop the optional loop-lag probe (call on shutdown)."""
        self._lag_probe.stop()

    def batch_scale_factor(self) -> float:
        base = min(self.memory.batch_scale_factor(), self.cpu.batch_scale_factor())
        # A lagging event loop is its own bottleneck regardless of cgroup CPU;
        # shed load proportionally when it falls behind its scheduled cadence.
        return base * self._lag_probe.factor()

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        """Block while *either* resource is saturated, bounded by the budget.

        One injected ``clock``, handed to both sub-gates, bounds this wait and
        theirs; it is read back off ``memory`` because that is the gate the
        budget comes from, so the two cannot come from different sources. See
        ``ResourceGauge.wait_while_saturated`` for why it is injectable at all.
        """
        budget = self.memory.max_wait_seconds if max_wait_seconds is None else max_wait_seconds
        deadline = self.memory.clock() + budget
        while True:
            mem_r = self.memory.usage_ratio()
            cpu_r = self.cpu.usage_ratio()
            if mem_r < self.memory.hard and cpu_r < self.cpu.hard:
                return
            if self.memory.clock() >= deadline:
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

_HTTP_TASK_RAM_BYTES = get_env_int("CRAWLUTRON_HTTP_TASK_RAM_BYTES", 48 * 1024 * 1024, lo=0)
# The 700 MiB default reservation sits BELOW the measured RSS range for a
# live renderer (~600-900 MiB once WebExtensions/Web Content are counted; see
# app/transport/browser_pool.py). That gap is deliberate, not an
# oversight, and this comment exists so it is not "corrected" by reflex: the
# reservation is the weight the weighted admission subtracts from the RAM
# budget per browser task, and raising it toward the top of the measured range
# lowers the number of concurrent browsers the admission will grant. Effective
# browser concurrency is a capacity decision that trades throughput against the
# hard container memory ceiling (docker-compose mem_limit; the failure mode is
# an OOM kill of the whole service), so the number stays conservative while the
# measured range straddles it. The right knob for that trade-off is here
# (CRAWLUTRON_BROWSER_TASK_RAM_BYTES), not a change to the default.
_BROWSER_TASK_RAM_BYTES = get_env_int("CRAWLUTRON_BROWSER_TASK_RAM_BYTES", 700 * 1024 * 1024, lo=0)
# Persistent markitdown workers (app.crawl.extraction._DocWorker): each imports
# markitdown plus the PDF/Office parsers once and stays alive for the process, so
# this is a FIXED reservation, not a per-task estimate. Measured footprint is
# 100-300 MiB apiece depending on which parsers get touched; the conservative
# end is the default because the container ceiling is hard (docker-compose
# mem_limit) and the failure mode is an OOM kill of the whole service.
_DOC_TASK_RAM_BYTES = get_env_int("CRAWLUTRON_DOC_TASK_RAM_BYTES", 300 * 1024 * 1024, lo=0)
# Default persistent markitdown worker count; mirrors the 3 GiB container
# ceiling the compose pins. Overridable via CRAWLUTRON_EXTRACTION_DOC_WORKERS.
_DEFAULT_DOC_WORKERS = 2
_ADMIT_YIELD_SECONDS = get_env_float("CRAWLUTRON_ADMIT_YIELD_SECONDS", 0.05, lo=0.0)
_MAX_HTTP_CONCURRENCY = get_env_int("CRAWLUTRON_MAX_HTTP_CONCURRENCY", 512, lo=1)


# --- Shared RAM-budget sizing (browser pool + httpx pool derive from this) ---
# Both the browser pool and the HTTP connection pool size themselves from the cgroup
# memory limit, centralized here (import-safe from settings) as the single source of
# truth so the connection ceiling tracks the weighted admission's HTTP concurrency.
_BROWSER_RAM_BYTES = get_env_int("CRAWLUTRON_BROWSER_RAM_BYTES", 700 * 1024 * 1024)
BROWSER_RAM_FRACTION = get_env_float("CRAWLUTRON_BROWSER_RAM_FRACTION", 0.5, lo=0.0, hi=1.0)
_MAX_DERIVED_POOL_SIZE = get_env_int("CRAWLUTRON_MAX_DERIVED_POOL_SIZE", 4)


def worker_count() -> int:
    """How many uvicorn worker processes this deployment runs.

    The single home for the value. It lives here rather than in ``app.main``
    because the functions that size a process's pools live here, and a pool
    sized from a shared cgroup limit has to know how many processes are
    spending it -- so importing the number from the module that refuses N>1
    would be circular.

    Reads through the never-raise env chokepoint, so a typo'd value falls back
    to one worker instead of dividing every budget to nothing.
    """
    return get_env_int("CRAWLUTRON_WORKERS", 1, lo=1)


def doc_worker_count() -> int:
    """Persistent markitdown worker count *for this process*.

    The single home for this count: ``app.crawl.extraction`` borrows it so the
    pool it spawns is exactly the size admission reserved. Reading the env var
    in two places would let the spawned pool and the RAM reservation drift
    apart. A malformed value falls back to the default rather than aborting
    startup -- extraction is a nicety, not a load-bearing path.

    The configured count is a fleet total: each worker spawns its own markitdown
    process pool, and each holds 100-300 MiB for the life of the process, so N
    workers at the configured count would be N times the reserved RAM.
    """
    try:
        configured = get_env_int("CRAWLUTRON_EXTRACTION_DOC_WORKERS", _DEFAULT_DOC_WORKERS, lo=1)
    except ValueError:
        configured = _DEFAULT_DOC_WORKERS
    return max(1, configured // worker_count())


def derive_browser_pool_size(limit_bytes: int | None = None) -> int:
    """Browser-pool size *for this process*, from the cgroup memory limit.

    Honors ``CRAWLUTRON_BROWSER_POOL_SIZE``; else derives so this process's
    share of the fleet uses at most ``BROWSER_RAM_FRACTION`` of RAM; falls back
    to 1 when no limit is discoverable. Shared by
    ``app.transport.browser_pool`` and the httpx pool sizing so the
    connection ceiling tracks the admission's HTTP concurrency.

    The cgroup limit belongs to the container, so it is the same number in every
    worker while the pool is each worker's own. Un-divided, N workers would each
    spend the whole budget. The configured size is therefore a fleet total too,
    which coincides with the previous per-process reading in the single-worker
    configuration that ships.
    """
    workers = worker_count()
    explicit = get_env_int("CRAWLUTRON_BROWSER_POOL_SIZE", 0, lo=0)
    if explicit > 0:
        return max(1, explicit // workers)
    limit = limit_bytes if limit_bytes is not None else read_memory_stats().total_bytes
    if limit and limit > 0:
        # The cap is a fleet ceiling, not a per-process one. Left undivided it
        # is the worse half of the bug: a large container would let N workers
        # each take the full cap, which is N times the number it names.
        cap = max(1, _MAX_DERIVED_POOL_SIZE // workers)
        budget = int(limit * BROWSER_RAM_FRACTION) // workers
        size = max(1, budget // _BROWSER_RAM_BYTES)
        return min(size, cap)
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
    """Admission's HTTP concurrency ceiling *for this process*.

    Reserves this process's fixed pools' RAM, then divides the remainder by the
    per-task estimate. This is exactly the ceiling ``WeightedAdmission`` uses, so
    the httpx pool can be bound to it with zero drift as RAM scales.

    The remainder is this worker's share: the limit is the container's and
    ``pool_size`` and ``doc_workers`` are per-process, so the reserve is a
    *fleet* quantity and is multiplied before the subtraction. Subtracting it
    once and then dividing overstates the fleet's free memory -- with 4 workers
    on a 16 GiB limit that is 320 concurrent HTTP tasks where a single worker
    would have run 283, which is the over-admission the division exists to
    remove.
    """
    workers = worker_count()
    limit = max(limit_bytes, 256 * 1024 * 1024)
    per_worker_reserve = pool_size * browser_task_bytes + doc_workers * doc_task_bytes
    free = max(0, limit - workers * per_worker_reserve) // workers
    return max(1, min(_MAX_HTTP_CONCURRENCY, free // max(1, http_task_bytes)))


@dataclass(frozen=True)
class TaskRamBudget:
    """The per-task RAM weights admission sizing is derived from, as one value.

    ``build_admission_policy`` and ``max_http_concurrency_for`` sized a
    process's admission from the same four inputs, and the duplication was those
    two keyword-only parameter lists rather than any logic: the
    duplication was the argument list, not the arithmetic. Binding the list into
    a value the caller builds and this function hands on unchanged means the
    weights exist once, and a caller adding a weight edits a dataclass instead
    of two signatures that have to agree. The same move
    ``app.crawl.result_helpers.TerminalResult`` made for the terminal-result
    constructors, for the same reason.

    Every field defaults to the env-read weight this module already reserves
    against, so admission built without a budget reserves exactly what it did
    before. ``doc_workers`` defaults to zero rather than to
    :func:`doc_worker_count` because a caller that spawns no markitdown pool
    must not have one silently reserved for it -- the count is a fact about the
    caller's process, not a default the sizing can know.
    """

    http_task_bytes: int = _HTTP_TASK_RAM_BYTES
    browser_task_bytes: int = _BROWSER_TASK_RAM_BYTES
    doc_workers: int = 0
    doc_task_bytes: int = _DOC_TASK_RAM_BYTES


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
    # hold. The browser tier must not shrink against its own reserved usage;
    # only surplus usage beyond this fraction yields browser capacity.
    browser_ram_fraction: float = BROWSER_RAM_FRACTION


def build_admission_policy(
    limit_bytes: int,
    *,
    pool_size: int,
    max_concurrent_pages: int,
    budget: TaskRamBudget | None = None,
) -> AdmissionPolicy:
    """Derive an admission policy from the memory limit plus the fixed pools.

    The four sizing weights arrive as one :class:`TaskRamBudget` rather than as
    four parameters, because they were four parameters here *and* four there and
    the two lists had to be edited together. The budget is passed to
    :func:`max_http_concurrency_for` unchanged, so the HTTP ceiling this policy
    reports is computed from exactly the weights the policy was built from --
    the property the flat signature made a coincidence of ordering.
    """
    sizing = budget or TaskRamBudget()
    limit = max(limit_bytes, 256 * 1024 * 1024)
    return AdmissionPolicy(
        http_task_bytes=sizing.http_task_bytes,
        browser_task_bytes=sizing.browser_task_bytes,
        browser_pool_reserve_bytes=pool_size * sizing.browser_task_bytes,
        max_http_concurrency=max_http_concurrency_for(
            limit,
            pool_size,
            http_task_bytes=sizing.http_task_bytes,
            browser_task_bytes=sizing.browser_task_bytes,
            doc_workers=sizing.doc_workers,
            doc_task_bytes=sizing.doc_task_bytes,
        ),
        max_browser_concurrency=max(1, pool_size * max_concurrent_pages),
        doc_pool_reserve_bytes=sizing.doc_workers * sizing.doc_task_bytes,
    )


def default_admission_policy(gate: ResourceGate) -> WeightedAdmission:
    """A conservative policy used when the caller does not supply one.

    Assumes a 2-process browser pool and the default doc worker count, so tests
    and minimal deployments stay safe; production passes the real pools.
    """
    stats = read_memory_stats()
    limit = stats.total_bytes or (4 * 1024 * 1024 * 1024)
    return WeightedAdmission(
        gate,
        build_admission_policy(
            limit,
            pool_size=2,
            max_concurrent_pages=3,
            budget=TaskRamBudget(doc_workers=doc_worker_count()),
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
        # must not shrink against its own reserved usage (double scaling).
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
