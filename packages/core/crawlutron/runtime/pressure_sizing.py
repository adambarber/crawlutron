"""Resource-adaptive admission control: CPU, loop lag, workers, policy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from admission import (
    AdmissionGate,
    AdmissionRefused,
    Bytes,
    Cost,
    Count,
    FloorDivisor,
    Instant,
    LaneId,
    LaneSpec,
    Limits,
    MinAll,
    Multiplier,
    PacingPolicy,
    Ratio,
    ResourceId,
    Sensing,
    SensorBinding,
    Signal,
    Want,
)
from admission.gauge import ResourceGauge as KernelResourceGauge

from crawlutron.config.env import get_env_float, get_env_int
from crawlutron.runtime.pressure_gates import CpuGate, ResourceGate
from crawlutron.runtime.pressure_memory import read_memory_stats
from crawlutron.runtime.pressure_probes import LoopLagProbe, read_cpu_quota, read_cpu_usage_seconds

__all__ = [
    "CpuGate",
    "LoopLagProbe",
    "ResourceGate",
    "build_admission_gate",
    "default_executor_workers",
    "kernel_admit",
    "kernel_lane_specs",
    "read_cpu_quota",
    "read_cpu_usage_seconds",
]


# --- Two-tier weighted admission ---

_HTTP_TASK_RAM_BYTES = get_env_int("CRAWLUTRON_HTTP_TASK_RAM_BYTES", 48 * 1024 * 1024, lo=0)


# Deliberately below the measured 600-900 MiB renderer RSS (see browser_pool.py):
# it is the admission weight per browser task, so raising it lowers concurrency.
_BROWSER_TASK_RAM_BYTES = get_env_int("CRAWLUTRON_BROWSER_TASK_RAM_BYTES", 700 * 1024 * 1024, lo=0)


# Fixed per-process reservation (markitdown + parsers load once, live forever);
# conservative end of the measured 100-300 MiB because the ceiling OOM-kills.
_DOC_TASK_RAM_BYTES = get_env_int("CRAWLUTRON_DOC_TASK_RAM_BYTES", 300 * 1024 * 1024, lo=0)


# Default persistent markitdown worker count; mirrors the 3 GiB container
# ceiling the compose pins. Overridable via CRAWLUTRON_EXTRACTION_DOC_WORKERS.
_DEFAULT_DOC_WORKERS = 2


# The shared asyncio.to_thread pool: the interpreter's implicit default is
# min(32, cpu+4) queued unboundedly; the floor keeps small boxes responsive.
_DEFAULT_EXECUTOR_FLOOR = 8
_DEFAULT_EXECUTOR_CAP = 32


def default_executor_workers() -> int:
    """Thread count for the loop's shared default executor, env-overridable."""
    cpus = read_cpu_quota()
    if cpus is None:
        import os

        cpus = os.cpu_count() or 4
    derived = min(_DEFAULT_EXECUTOR_CAP, int(cpus) + 4)
    try:
        configured = get_env_int("CRAWLUTRON_DEFAULT_EXECUTOR_WORKERS", derived, lo=1)
    except ValueError:
        configured = _DEFAULT_EXECUTOR_FLOOR
    return max(_DEFAULT_EXECUTOR_FLOOR, min(_DEFAULT_EXECUTOR_CAP, configured))


_MAX_HTTP_CONCURRENCY = get_env_int("CRAWLUTRON_MAX_HTTP_CONCURRENCY", 512, lo=1)


# --- Shared RAM-budget sizing (browser pool + httpx pool derive from this) ---
# One source for both pools so the connection ceiling tracks admission's ceiling.
_BROWSER_RAM_BYTES = get_env_int("CRAWLUTRON_BROWSER_RAM_BYTES", 700 * 1024 * 1024)


BROWSER_RAM_FRACTION = get_env_float("CRAWLUTRON_BROWSER_RAM_FRACTION", 0.5, lo=0.0, hi=1.0)


MAX_DERIVED_POOL_SIZE = get_env_int("CRAWLUTRON_MAX_DERIVED_POOL_SIZE", 4)


def worker_count() -> int:
    """How many uvicorn worker processes this deployment runs -- the single home for
    the value (a pool sized from a cgroup limit must know how many processes spend it).
    Reads through the never-raise env chokepoint, so a bad value falls back to one worker.
    """
    return get_env_int("CRAWLUTRON_WORKERS", 1, lo=1)


def doc_worker_count() -> int:
    """Persistent markitdown worker count *for this process* -- the single home,
    so the spawned pool and the RAM reservation can't drift. The knob is per
    process (a fleet total is configured x worker_count(), exactly).
    """
    try:
        return get_env_int("CRAWLUTRON_EXTRACTION_DOC_WORKERS", _DEFAULT_DOC_WORKERS, lo=1)
    except ValueError:
        return _DEFAULT_DOC_WORKERS


def derive_browser_pool_size(limit_bytes: int | None = None) -> int:
    """Browser-pool size *for this process* from the cgroup memory limit: honors
    CRAWLUTRON_BROWSER_POOL_SIZE, else derives this process's share up to
    BROWSER_RAM_FRACTION, falling back to 1; the cap is a fleet total, not per process.
    """
    workers = worker_count()
    explicit = get_env_int("CRAWLUTRON_BROWSER_POOL_SIZE", 0, lo=0)
    if explicit > 0:
        return max(1, explicit // workers)
    limit = limit_bytes if limit_bytes is not None else read_memory_stats().total_bytes
    if limit and limit > 0:
        # A fleet ceiling, not per-process: undivided, N workers each take
        # the full cap, which is N times the number it names.
        cap = max(1, MAX_DERIVED_POOL_SIZE // workers)
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
    """Admission's HTTP concurrency ceiling *for this process*: reserve the fixed
    pools' RAM, then divide the remainder by the per-task estimate. The reserve
    is a fleet quantity, multiplied before the division so this worker's share stays honest.
    """
    workers = worker_count()
    limit = max(limit_bytes, 256 * 1024 * 1024)
    per_worker_reserve = pool_size * browser_task_bytes + doc_workers * doc_task_bytes
    free = max(0, limit - workers * per_worker_reserve) // workers
    return max(1, min(_MAX_HTTP_CONCURRENCY, free // max(1, http_task_bytes)))


@dataclass(frozen=True)
class TaskRamBudget:
    """The per-task RAM weights admission sizing is derived from, as one value.
    The weights used to be two keyword-only parameter lists that had to agree;
    binding them into one value means adding a weight edits a dataclass, not two signatures.
    """

    http_task_bytes: int = _HTTP_TASK_RAM_BYTES
    browser_task_bytes: int = _BROWSER_TASK_RAM_BYTES
    doc_workers: int = 0
    doc_task_bytes: int = _DOC_TASK_RAM_BYTES


@dataclass
class AdmissionPolicy:
    """Sizing inputs for the kernel gate's lane specs (all in bytes / counts)."""

    http_task_bytes: int
    browser_task_bytes: int
    browser_pool_reserve_bytes: int
    max_http_concurrency: int
    max_browser_concurrency: int
    # The doc pool is a fixed RAM consumer like the browser pool; surfaced
    # separately so a caller sees spend committed before any task is admitted.
    doc_pool_reserve_bytes: int = 0
    # The RAM fraction derive_browser_pool_size assumed the pool may hold; the
    # tier never shrinks against its own reserve, only surplus yields capacity.
    browser_ram_fraction: float = BROWSER_RAM_FRACTION


def build_admission_policy(
    limit_bytes: int,
    *,
    pool_size: int,
    max_concurrent_pages: int,
    budget: TaskRamBudget | None = None,
) -> AdmissionPolicy:
    """Derive an admission policy from the memory limit plus the fixed pools.
    The four sizing weights arrive as one ``TaskRamBudget`` (they were four
    parameters in two places); the budget passes through to the HTTP ceiling unchanged.
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


def kernel_lane_specs(policy: AdmissionPolicy, budget: TaskRamBudget) -> dict[LaneId, LaneSpec]:
    """The two kernel LaneSpecs for one sized policy (C3, seam-only).

    HTTP->GENERAL, BROWSER->HEAVY (spec 2); ceilings are the RAM-derived tier
    caps and per-unit costs are the task RAM weights, all in bytes.
    """
    return {
        LaneId.GENERAL: LaneSpec(
            ceiling=Count(policy.max_http_concurrency),
            cost=Cost(Bytes(budget.http_task_bytes)),
            scale_source=MinAll(),
        ),
        LaneId.HEAVY: LaneSpec(
            ceiling=Count(policy.max_browser_concurrency),
            cost=Cost(Bytes(budget.browser_task_bytes)),
            scale_source=MinAll(),
        ),
    }


class _GaugeSurface(Protocol):
    """The seam's structural gate door: the ratio surface satisfied by both the
    real ResourceGate and the pure FakeResourceGauge."""

    def memory_ratio(self) -> float: ...

    def cpu_ratio(self) -> float: ...


class _SeamGauge(KernelResourceGauge):
    """A kernel gauge over one seam ratio reader, always-warm (v1 seam posture).

    The seam's ResourceGate reads current cgroup ratios; the kernel's cold/stale
    machinery stays test-side. Warm + current means no cold cap and no STALE park."""

    def __init__(self, ratio: Callable[[], float]) -> None:
        self._ratio = ratio

    def is_warm(self) -> bool:
        return True

    def read_usage(self) -> Ratio:
        return Ratio(max(0.0, min(1.0, self._ratio())))

    def last_sample_age(self, now: float) -> float:
        return 0.0


class _MonotonicClock:
    """The kernel's monotonic Clock bound to the real monotonic timeline."""

    def now(self) -> Instant:
        return Instant._of(time.monotonic())


def _seam_signal() -> Signal:
    """A fresh asyncio-backed Signal for one kernel gate."""

    class _EventSignal:
        def __init__(self) -> None:
            self._event = asyncio.Event()

        async def wait(self, timeout: float | None = None) -> bool:
            try:
                await asyncio.wait_for(self._event.wait(), timeout)
                return True
            except TimeoutError:
                return False

        def set(self) -> None:
            self._event.set()

    return _EventSignal()


def _subgate_limits(gate: _GaugeSurface, which: str, default_target: float, default_hard: float) -> Limits:
    """The seam sub-gate's target/hard, or sane defaults on a gauge double."""
    sub = getattr(gate, which, None)
    if sub is not None and hasattr(sub, "target") and hasattr(sub, "hard"):
        return Limits(target=Ratio(sub.target), hard=Ratio(sub.hard))
    return Limits(target=Ratio(default_target), hard=Ratio(default_hard))


def build_admission_gate(
    gate: _GaugeSurface,
    policy: AdmissionPolicy | None = None,
    budget: TaskRamBudget | None = None,
) -> AdmissionGate:
    """The kernel gate for one crawl, adapting the seam's ResourceGate.

    With no policy, a conservative one is derived (2-process browser pool, default
    doc workers) so tests and minimal deployments stay safe."""
    sizing = budget or TaskRamBudget(doc_workers=doc_worker_count())
    if policy is None:
        stats = read_memory_stats()
        limit = stats.total_bytes or (4 * 1024 * 1024 * 1024)
        policy = build_admission_policy(
            limit,
            pool_size=2,
            max_concurrent_pages=3,
            budget=sizing,
        )
    sensing = Sensing(
        {
            ResourceId.MEMORY: SensorBinding(
                _subgate_limits(gate, "memory", 0.85, 0.90),
                _SeamGauge(gate.memory_ratio),
            ),
            ResourceId.CPU: SensorBinding(
                _subgate_limits(gate, "cpu", 0.90, 0.95),
                _SeamGauge(gate.cpu_ratio),
            ),
        }
    )
    lanes = kernel_lane_specs(policy, sizing)
    # A ramp pre-warmed to 16x the widest ceiling keeps the unsaturated band
    # granting fully (seam parity); saturation pacing comes from free slots.
    warm = 16 * max(policy.max_http_concurrency, policy.max_browser_concurrency)
    pacing = PacingPolicy(
        ramp_floor=Count(warm),
        ramp_max=Count(warm),
        ramp_multiplier=Multiplier(1.10),
        floor_fraction=FloorDivisor(4),
        initial_ramp=Count(warm),
    )
    # The budget covers a fully-saturated box, so the kernel's memory term never
    # binds below the RAM-derived ceilings (which already encode capacity).
    fully_loaded = (
        policy.max_http_concurrency * sizing.http_task_bytes
        + policy.max_browser_concurrency * sizing.browser_task_bytes
    )
    return AdmissionGate(
        pacing=pacing,
        lanes=lanes,
        sensing=sensing,
        clock=_MonotonicClock(),
        signal=_seam_signal(),
        memory_budget=Bytes(max(fully_loaded, 1)),
    )


async def kernel_admit(gate: AdmissionGate, want_http: int, want_browser: int) -> tuple[int, int]:
    """Admit a batch through the kernel gate; a refusal (OVERSIZE) yields (0, 0).
    The kernel parks internally under pressure and grants a floor >= 1."""
    if want_http + want_browser < 1:
        return 0, 0
    want = Want.of(general=Count(want_http), heavy=Count(want_browser))
    try:
        async with gate.admit(want) as granted:
            return (
                granted.allocation.for_lane(LaneId.GENERAL).value,
                granted.allocation.for_lane(LaneId.HEAVY).value,
            )
    except AdmissionRefused:
        return 0, 0
