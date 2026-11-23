"""Shared UTC-normalization helpers for HTTP-date/timestamp conversion."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_timestamp(dt: datetime | None) -> float | None:
    """Seconds since epoch, reading naive datetime as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def http_date_epoch(value: str | None) -> float | None:
    """Epoch seconds for an HTTP date header, or None if unparsable."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        return utc_timestamp(parsedate_to_datetime(value))
    except (TypeError, ValueError, OverflowError):
        return None


__all__ = ["http_date_epoch", "utc_timestamp"]
