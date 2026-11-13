"""Mapped foreign ValueErrors: ``(foreign_message,)`` -> count.

The bounded record of constructor-belt remaps, so a bug is not silently 422.
"""

from __future__ import annotations

import safe_url.process_state as process_state

_RECORD: dict[tuple[str], int] = {}


def record_mapped_value_error(foreign_message: str) -> None:
    """Tally one foreign-ValueError remap; structured fields only, never a sink."""
    key = (foreign_message,)
    _RECORD[key] = _RECORD.get(key, 0) + 1


def mapped_value_errors() -> tuple:
    """Snapshot of every mapped foreign error, sorted for stable comparison."""
    return _error_snapshot()


def _error_snapshot() -> tuple[tuple[str, ...], ...]:
    items = list(_RECORD.items())
    items.sort()
    return tuple(key for key, _ in items)


def clear_mapped_value_errors() -> None:
    """Reset door: tests empty the record between cases."""
    _RECORD.clear()


def _has_mapped_value_errors() -> bool:
    """Whether any mapped value errors are recorded right now."""
    return len(_RECORD) > 0


process_state.register(__name__, clear_mapped_value_errors, _has_mapped_value_errors)


__all__ = [
    "clear_mapped_value_errors",
    "mapped_value_errors",
    "record_mapped_value_error",
]
