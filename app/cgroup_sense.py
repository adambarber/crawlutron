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
# The value type is ``str | None`` and nothing else: ``_read_text`` is the only
# writer, and it stores either a stripped read or None. It was ``object``, which
# is not a wider truth but a lost one -- the annotation then said nothing about
# what a reader gets back, so returning it needed a `# type: ignore[return-value]`
# to be legal and the callers' own `str | None` contract went unchecked. Declaring
# the value is what let both checkers read the code instead of the suppression.
_CGROUP_CACHE: dict[str, tuple[float, str | None]] = {}


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
            return value
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
