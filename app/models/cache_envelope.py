"""The on-disk record one cached entry serializes to.

A cache file is this service's own write, read back on the next boot by the
same module -- so it is a declared record, not arbitrary JSON. ``value`` is the
caller's payload and is genuinely arbitrary; the other two fields are the
freshness contract, and every expiry comparison in the cache depends on both
being present.
"""

from __future__ import annotations

from typing import TypedDict

from app.models.json_value import JsonValue


class CacheEnvelope(TypedDict):
    expires: float | None
    value: JsonValue
    cached_at: float


__all__ = ["CacheEnvelope"]
