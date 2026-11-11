"""Key-verification metering (#653): the pre-auth PBKDF2 amplifier is throttled.

Every X-API-Key request costs one PBKDF2-SHA256 at 210k iterations; consecutive
failed verifies trip a fail-closed cooldown (#621). #675: FULL-secret buckets.
"""

from __future__ import annotations

import hashlib
import time

from crawlutron.service.errors import DomainError, KeyVerifyThrottled
from crawlutron.service.meter_ledger import MeterLedger, register_shared_meter

__all__ = ["KeyVerifyThrottle", "shared_throttle"]

_FAILURE_WINDOW_SECONDS = 300.0
_COOLDOWN_SECONDS = 60.0
#: The LRU bound (#675, AnonymousThrottle precedent): distinct-garbage minting
#: is bounded by capacity; cooling buckets are eviction-exempt.
_BUCKET_CAPACITY = 1024


def _throttled() -> DomainError:
    """The caller-visible metering refusal: a 429 key-verify refusal."""
    return KeyVerifyThrottled()


class _Bucket:
    """One presented-credential's failure ledger under the meter's lock."""

    __slots__ = ("cooldown_until", "failures", "window_started")

    def __init__(self) -> None:
        self.failures = 0
        self.window_started = time.monotonic()
        self.cooldown_until = 0.0


class KeyVerifyThrottle(MeterLedger[_Bucket]):
    """Fail-closed cooldown over failed key verifications, one chokepoint.

    Buckets key on a hash of the FULL presented secret (#675): poisoning requires
    the victim's secret itself. LRU-capped; a cooling bucket is never evicted.
    """

    MAX_FAILURES = 5
    #: The dedicated bucket every /v1/keys admin-token guess meters against
    #: (#659): one shared ledger minting cannot dilute.
    ADMIN_BUCKET = "admin"

    def check(self, presented: str) -> None:
        """Refuse while the credential's bucket is cooling; a caller-readable
        429-shaped refusal, raised by the auth wrapper, not the store."""
        self._check_bucket(self._key_for(presented))

    def record_failure(self, presented: str) -> None:
        """Count one failed verify; the Nth consecutive failure within the
        window starts the cooldown (fail-closed: even the live key refuses)."""
        self._record_bucket_failure(self._key_for(presented))

    def record_success(self, presented: str) -> None:
        """A hit clears the bucket: one fat-finger is not a hair-trigger."""
        with self._lock:
            self._buckets.pop(self._key_for(presented), None)

    def check_admin(self) -> None:
        """Refuse while the dedicated admin bucket is cooling (#659)."""
        self._check_bucket(self.ADMIN_BUCKET)

    def record_admin_failure(self) -> None:
        """Count one failed admin-token guess against the shared admin bucket."""
        from crawlutron.runtime.metrics import get_metrics

        get_metrics().record_admin_auth_failure()
        self._record_bucket_failure(self.ADMIN_BUCKET)

    def record_admin_success(self) -> None:
        """A correct admin token clears the admin bucket (not a hair-trigger)."""
        with self._lock:
            self._buckets.pop(self.ADMIN_BUCKET, None)

    def residence(self) -> int:
        """Live bucket count: the bounded-ledger probe door (test/probe)."""
        with self._lock:
            return len(self._buckets)

    def _check_bucket(self, key: str) -> None:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return
            self._buckets.move_to_end(key)
            now = self._clock()
        if bucket.cooldown_until > now:
            from crawlutron.runtime.metrics import get_metrics

            get_metrics().record_key_verify_throttle_trip()
            raise _throttled()

    def _locked_get_or_mint(self, key: str, now: float) -> _Bucket:
        """Under the lock: the bucket for key, or an LRU-priced new one."""
        bucket = self._buckets.get(key)
        if bucket is not None:
            self._buckets.move_to_end(key)
            return bucket
        self._evict_lru(now)
        bucket = _Bucket()
        bucket.window_started = now
        self._buckets[key] = bucket
        return bucket

    def _evict_lru(self, now: float) -> None:
        """Drop least-recently-used buckets at capacity; cooling buckets are
        exempt, so a flood can never launder a cooldown victim's ledger."""
        while len(self._buckets) >= _BUCKET_CAPACITY:
            lru_key = next(
                (k for k, b in self._buckets.items() if b.cooldown_until <= now),
                None,
            )
            if lru_key is None:
                return
            del self._buckets[lru_key]

    def _record_bucket_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            bucket = self._locked_get_or_mint(key, now)
            # #661: an EXPIRED cooldown re-arms the ladder -- clear the bucket
            # under the lock, so one stale failure cannot re-cool instantly.
            if bucket.cooldown_until and bucket.cooldown_until <= now:
                bucket.failures = 0
                bucket.window_started = now
                bucket.cooldown_until = 0.0
            self._record_failure_into(bucket, now)

    def _record_failure_into(self, bucket: _Bucket, now: float) -> None:
        """Mutate one bucket's failure ladder under ``self._lock``.

        Pure bucket+now mutation (no I/O, no await) -- safe under the lock;
        concurrent failures share one bucket, so the lock must cover it."""
        if now - bucket.window_started > _FAILURE_WINDOW_SECONDS:
            bucket.failures = 0
            bucket.window_started = now
        bucket.failures += 1
        if bucket.failures >= self.MAX_FAILURES:
            bucket.cooldown_until = now + _COOLDOWN_SECONDS

    def _key_for(self, presented: str) -> str:
        """The bucket key: a SHA-256 of the FULL presented secret (#675)."""
        return hashlib.sha256(presented.encode("utf-8")).hexdigest()


_SHARED = KeyVerifyThrottle()


def shared_throttle() -> KeyVerifyThrottle:
    """The one meter the auth chokepoint uses (per-entry-point, not per-door)."""
    return _SHARED


register_shared_meter(__name__, _SHARED)
