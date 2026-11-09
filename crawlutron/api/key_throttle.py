"""Key-verification metering (#653): the pre-auth PBKDF2 amplifier is throttled.

Every request presenting an ``X-API-Key`` costs one PBKDF2-SHA256 at 210k
iterations -- the service's most expensive pre-auth operation. This module
meters it: consecutive failed verifies from the same presented credential trip
a fail-closed cooldown. It is process state like every other holder (#621).

#659 anti-laundering control: the global MINT BUDGET. A distinct-credential
flood cannot mint unlimited buckets (512 per 300s), so it can never push a
cooldown victim's bucket out of the ledger. #663 bounds residence: when the
mint window rolls, buckets whose failure window AND cooldown have both lapsed
are swept -- one window's worth of live+cooling buckets is the steady state.
Accepted shared fate: once the budget is spent, EVERY first-time caller for
the window remainder is 429-refused -- fail-closed by choice.
"""

from __future__ import annotations

import time

from crawlutron.api.meter_ledger import MeterLedger, register_shared_meter

__all__ = ["KeyVerifyThrottle", "shared_throttle"]

_FAILURE_WINDOW_SECONDS = 300.0
_COOLDOWN_SECONDS = 60.0
#: Global bucket-mint pricing (#659) -- THE anti-laundering control: at most
#: this many NEW buckets per window, so a distinct-id flood is priced, not
#: free, and bucket count stays bounded below the (deleted) eviction cap.
_MINT_BUDGET = 512
_MINT_WINDOW_SECONDS = 300.0


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


class KeyVerifyThrottle(MeterLedger[_Bucket]):
    """Fail-closed cooldown over failed key verifications, one chokepoint.

    Buckets are keyed on the presented credential's parseable key id; anything
    unparseable or overlong pools into one fixed ``unknown`` bucket, so varying
    garbage cannot mint buckets. There is NO eviction (#659): the global mint
    budget is the anti-laundering control -- a flood cannot mint enough new
    buckets to displace a cooldown victim's ledger (#659), and #663 sweeps
    lapsed non-cooling buckets at each mint-window roll, so residence is
    capped at one window's live+cooling buckets.
    """

    MAX_FAILURES = 5
    UNKNOWN_BUCKET = "unknown"
    #: The dedicated bucket every /v1/keys admin-token guess meters against
    #: (#659): one shared ledger, so per-credential bucket minting cannot
    #: dilute the admin door's pacing. Accepted griefing risk: 5 wrong
    #: guesses lock the OPERATOR's correct token out for 60s (refreshable) --
    #: the alternative, per-source buckets, would re-mint unbounded bucket
    #: space on the one door that must not be dilutable, and a mint flood
    #: would then block admin failure-recording entirely.
    ADMIN_BUCKET = "admin"

    def __init__(self, *, clock=time.monotonic) -> None:
        super().__init__(clock=clock)
        self._mint_window_started = clock()
        self._minted = 0

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
        self._record_bucket_failure(self.ADMIN_BUCKET)

    def record_admin_success(self) -> None:
        """A correct admin token clears the admin bucket (not a hair-trigger)."""
        with self._lock:
            self._buckets.pop(self.ADMIN_BUCKET, None)

    def _mint_bucket(self, key: str, now: float) -> _Bucket:
        """Mint a NEW bucket against the global budget (#659); a refused mint
        raises the shared 429 shape, so a distinct-id flood is priced."""
        if now - self._mint_window_started > _MINT_WINDOW_SECONDS:
            self._mint_window_started = now
            self._minted = 0
            self._sweep_lapsed(now)
        if self._minted >= _MINT_BUDGET:
            raise _throttled()
        self._minted += 1
        bucket = _Bucket()
        bucket.window_started = now
        self._buckets[key] = bucket
        return bucket

    def residence(self) -> int:
        """Live bucket count: the ledger residence #663 caps (test/probe door)."""
        with self._lock:
            return len(self._buckets)

    def _sweep_lapsed(self, now: float) -> None:
        """#663 residence cap: drop buckets whose failure window AND cooldown
        have both lapsed; cooling victims are retained (#659)."""
        lapsed = [
            key
            for key, bucket in self._buckets.items()
            if bucket.cooldown_until < now and now - bucket.window_started > _FAILURE_WINDOW_SECONDS
        ]
        for key in lapsed:
            del self._buckets[key]

    def _check_bucket(self, key: str) -> None:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return
            now = self._clock()
        if bucket.cooldown_until > now:
            raise _throttled()

    def _locked_get_or_mint(self, key: str, now: float) -> _Bucket:
        """Under the lock: the bucket for key, or a mint-priced new one."""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._mint_bucket(key, now)
        return bucket

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
        if now - bucket.window_started > _FAILURE_WINDOW_SECONDS:
            bucket.failures = 0
            bucket.window_started = now
        bucket.failures += 1
        if bucket.failures >= self.MAX_FAILURES:
            bucket.cooldown_until = now + _COOLDOWN_SECONDS

    def _key_for(self, presented: str) -> str:
        prefix, sep, rest = presented.partition(".")
        if sep and prefix.startswith("ck_") and len(prefix) <= 64 and "." not in rest:
            return prefix
        return self.UNKNOWN_BUCKET

    def _reset(self) -> None:
        self._buckets.clear()


_SHARED = KeyVerifyThrottle()


def shared_throttle() -> KeyVerifyThrottle:
    """The one meter the auth chokepoint uses (per-entry-point, not per-door)."""
    return _SHARED


register_shared_meter(__name__, _SHARED)
