"""Key-verification metering (#653): the pre-auth PBKDF2 amplifier is throttled.

Every request presenting an ``X-API-Key`` costs one PBKDF2-SHA256 at 210k
iterations -- the service's most expensive pre-auth operation. This module
meters it: consecutive failed verifies from the same presented credential trip
a fail-closed cooldown. It is process state like every other holder (#621).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from crawlutron.runtime import process_state

__all__ = ["KeyVerifyThrottle", "shared_throttle"]

_MAX_BUCKETS = 1024
_FAILURE_WINDOW_SECONDS = 300.0
_COOLDOWN_SECONDS = 60.0


def _throttled() -> Exception:
    """The caller-visible metering refusal: a 429-shaped HTTPException."""
    from crawlutron.api.exceptions import http_error

    return http_error(429, "key_verify_throttled", "Too many failed key verifications; retry later")


class _Bucket:
    """One presented-credential's failure ledger under the meter's lock."""

    __slots__ = ("cooldown_until", "failures", "window_started")

    def __init__(self) -> None:
        self.failures = 0
        self.window_started = time.monotonic()
        self.cooldown_until = 0.0


class KeyVerifyThrottle:
    """Fail-closed cooldown over failed key verifications, one chokepoint.

    Buckets are keyed on the presented credential's parseable key id; anything
    unparseable or overlong pools into one fixed ``unknown`` bucket, so varying
    garbage cannot mint buckets. LRU eviction resets ledgers, favoring victims.
    """

    MAX_FAILURES = 5
    UNKNOWN_BUCKET = "unknown"

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, presented: str) -> None:
        """Refuse while the credential's bucket is cooling; a caller-readable
        429-shaped refusal, raised by the auth wrapper, not the store."""
        bucket = self._bucket_for(presented)
        now = self._clock()
        if bucket.cooldown_until > now:
            raise _throttled()

    def record_failure(self, presented: str) -> None:
        """Count one failed verify; the Nth consecutive failure within the
        window starts the cooldown (fail-closed: even the live key refuses)."""
        bucket = self._bucket_for(presented)
        now = self._clock()
        if now - bucket.window_started > _FAILURE_WINDOW_SECONDS:
            bucket.failures = 0
            bucket.window_started = now
        bucket.failures += 1
        if bucket.failures >= self.MAX_FAILURES:
            bucket.cooldown_until = now + _COOLDOWN_SECONDS

    def record_success(self, presented: str) -> None:
        """A hit clears the bucket: one fat-finger is not a hair-trigger."""
        with self._lock:
            self._buckets.pop(self._key_for(presented), None)

    def _key_for(self, presented: str) -> str:
        prefix, sep, rest = presented.partition(".")
        if sep and prefix.startswith("ck_") and len(prefix) <= 64 and "." not in rest:
            return prefix
        return self.UNKNOWN_BUCKET

    def _bucket_for(self, presented: str) -> _Bucket:
        key = self._key_for(presented)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[key] = bucket
            self._buckets.move_to_end(key)
            while len(self._buckets) > _MAX_BUCKETS:
                self._buckets.popitem(last=False)
        return bucket

    def _reset(self) -> None:
        self._buckets.clear()

    def _has(self) -> bool:
        return bool(self._buckets)


_SHARED = KeyVerifyThrottle()


def shared_throttle() -> KeyVerifyThrottle:
    """The one meter the auth chokepoint uses (per-entry-point, not per-door)."""
    return _SHARED


def _reset_shared() -> None:
    _SHARED._reset()


def _has_shared() -> bool:
    return _SHARED._has()


process_state.register(__name__, _reset_shared, _has_shared)
