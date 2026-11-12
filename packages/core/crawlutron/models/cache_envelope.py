"""The on-disk record one cached entry serializes to.

A declared record, not arbitrary JSON: ``value`` is the caller's arbitrary payload;
the other two fields are the freshness contract every expiry comparison depends on.
"""

from __future__ import annotations

from typing import TypedDict

from crawlutron.models.json_value import JsonValue


class CacheEnvelope(TypedDict):
    expires: float | None
    value: JsonValue
    cached_at: float


__all__ = ["CacheEnvelope"]
