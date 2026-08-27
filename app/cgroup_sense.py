"""Minimal cgroup filesystem readers shared by the resource gauges.

Both :mod:`app.memory_pressure` and :mod:`app.resource_pressure` need to read
cgroup scalar files (limits, usage, quotas). These tiny readers were
duplicated across both modules; centralizing them removes the copy and keeps
the sensing logic in one place.
"""

from __future__ import annotations


def _read_text(path: str) -> str | None:
    """Read a cgroup scalar file, returning None on any access error."""
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    """Read a cgroup integer file; empty / ``max`` / non-integer -> None."""
    try:
        with open(path) as fh:
            value = fh.read().strip()
    except OSError:
        return None
    if not value or value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None
