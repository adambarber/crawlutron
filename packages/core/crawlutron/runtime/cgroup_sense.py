"""Minimal cgroup filesystem readers shared by the resource gauges.

The memory- and resource-pressure modules both read cgroup scalar files (limits,
usage, quotas); these readers were duplicated, so they now live here once.
"""

from __future__ import annotations

import time

from crawlutron.runtime import process_state

# Cached for a short window: cgroup reads happen on every dispatch tick and sample, and a
# burst makes them redundant syscalls without a window -- so it is deliberately short.
_CGROUP_READ_TTL_SECONDS = 0.1
# ``str | None`` and nothing else: ``object`` was a lost truth, not a wider one -- the
# annotation said nothing about what a reader gets, so a suppression made it legal.
_CGROUP_CACHE: dict[str, tuple[float, str | None]] = {}


def _reset() -> None:
    """Drop all cached cgroup reads."""
    _CGROUP_CACHE.clear()


def _has() -> bool:
    """Whether any cgroup reading is memoized right now."""
    return bool(_CGROUP_CACHE)


process_state.register(__name__, _reset, _has)


def read_text(path: str) -> str | None:
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


def read_int(path: str) -> int | None:
    """Read a cgroup integer file; empty / ``max`` / non-integer -> None.

    Results are memoized for :data:`_CGROUP_READ_TTL_SECONDS`.
    """
    text = read_text(path)
    if text is None:
        return None
    if not text or text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None
