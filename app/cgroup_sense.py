"""Minimal cgroup filesystem readers shared by the resource gauges.

Both :mod:`app.memory_pressure` and :mod:`app.resource_pressure` need to read
cgroup scalar files (limits, usage, quotas). These tiny readers were
duplicated across both modules; centralizing them removes the copy and keeps
the sensing logic in one place.
"""

from __future__ import annotations

import time

# cgroup reads are cheap individually but happen on every dispatch tick and on
# every memory/CPU sample; caching them for a short window (adversarial perf
# #2.3: "cache cgroup reads 50-200ms") removes redundant syscalls during a
# burst without making the gate stale. The window is deliberately short.
_CGROUP_READ_TTL_SECONDS = 0.1
_CGROUP_CACHE: dict[str, tuple[float, object]] = {}


def _invalidate_cgroup_cache() -> None:
    """Drop all cached cgroup reads (e.g. between tests)."""
    _CGROUP_CACHE.clear()


def _read_text(path: str) -> str | None:
    """Read a cgroup scalar file, returning None on any access error.

    Results are memoized for :data:`_CGROUP_READ_TTL_SECONDS`.
    """
    now = time.monotonic()
    cached = _CGROUP_CACHE.get(path)
    if cached is not None:
        ts, value = cached
        if now - ts < _CGROUP_READ_TTL_SECONDS:
            return value  # type: ignore[return-value]
    try:
        with open(path) as fh:
            value = fh.read().strip()
    except OSError:
        value = None
    _CGROUP_CACHE[path] = (now, value)
    return value


def _read_int(path: str) -> int | None:
    """Read a cgroup integer file; empty / ``max`` / non-integer -> None.

    Results are memoized for :data:`_CGROUP_READ_TTL_SECONDS`.
    """
    text = _read_text(path)
    if text is None:
        return None
    if not text or text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None
