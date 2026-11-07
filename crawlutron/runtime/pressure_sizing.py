"""Resource-adaptive admission control: CPU, loop lag, workers, policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from crawlutron.config.env import get_env_float, get_env_int
from crawlutron.runtime.pressure_gates import CpuGate, ResourceGate
from crawlutron.runtime.pressure_memory import read_memory_stats
from crawlutron.runtime.pressure_probes import LoopLagProbe, read_cpu_quota, read_cpu_usage_seconds

__all__ = [
    "CpuGate",
    "LoopLagProbe",
    "default_executor_workers",
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


_ADMIT_YIELD_SECONDS = get_env_float("CRAWLUTRON_ADMIT_YIELD_SECONDS", 0.05, lo=0.0)


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
    so the spawned pool and the RAM reservation can't drift. A malformed value
    falls back to the default; the count is a fleet total, each worker spawning its own pool.
    """
    try:
        configured = get_env_int("CRAWLUTRON_EXTRACTION_DOC_WORKERS", _DEFAULT_DOC_WORKERS, lo=1)
    except ValueError:
        configured = _DEFAULT_DOC_WORKERS
    return max(1, configured // worker_count())


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
    """Sizing inputs for :class:`WeightedAdmission` (all in bytes / counts)."""

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
    HTTP tasks are cheap and browser tasks expensive (the persistent browser pool
    holds most RAM), so tiers are budgeted separately; ``admit`` yields rather than parks the crawl.
    """

    def __init__(self, gate: ResourceGate, policy: AdmissionPolicy) -> None:
        self._gate = gate
        self._p = policy

    def headroom_fraction(self) -> float:
        return min(1.0 - self._gate.memory_ratio(), 1.0 - self._gate.cpu_ratio())

    def http_capacity(self) -> int:
        return max(0, int(self._p.max_http_concurrency * self.headroom_fraction()))

    def browser_capacity(self) -> int:
        # Full while usage stays within the fraction the pool sizing assumed,
        # then ramps to zero as the box exhausts; never admit when saturated.
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

    def saturated_dispatch_floor(self) -> int:
        """The per-pass dispatch floor under a saturated gate (#618): a quarter of
        the HTTP ramp, minimum one. A floor of one made a large batch under memory
        pressure pay one re-bucket + admit cycle per dispatched job."""
        return max(1, self._p.max_http_concurrency // 4)
