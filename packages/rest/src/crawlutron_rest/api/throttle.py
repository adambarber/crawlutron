"""Unified throttle chokepoint (#941): anonymous + key-verify metering.

Single class, single singleton, shared store — one metering plane for every
externally-reachable surface and every pre-auth key-verify path.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi.responses import JSONResponse

from crawlutron.runtime import process_state
from crawlutron.service.errors import DomainError, KeyVerifyThrottled
from crawlutron.service.meter_ledger import MeterLedger, register_shared_meter
from crawlutron.storage.coordination_store import atwin
from crawlutron.storage.coordination_throttle_store import CoordinationThrottleStore
from crawlutron_rest.api.credentials import is_operator, presented_key, remote_host

__all__ = [
    "ANON_MAX_REQUESTS",
    "Throttle",
    "Throttled",
    "shared_throttle",
    "throttle_anonymous_surfaces",
]

# Anonymous: 120 req / 60s per IP, batch 8.
ANON_MAX_REQUESTS = 120
_ANON_WINDOW = 60.0
_ANON_CAP = 1024
_ANON_NO_CLIENT = "unknown"

# Key-verify: 5 failures / 300s -> 60s cooldown.
_KV_WINDOW = 300.0
_KV_COOLDOWN = 60.0
_KV_CAP = 1024
_ADMIN_NO_CLIENT = "unknown"


class Throttled(Exception):
    """Typed refusal: no getattr dance at the refuse site."""

    def __init__(
        self,
        refusal_status: int,
        refusal_code: str,
        refusal_message: str,
        refusal_window_seconds: float = 0.0,
    ) -> None:
        super().__init__(refusal_message)
        self.refusal_status = refusal_status
        self.refusal_code = refusal_code
        self.refusal_message = refusal_message
        self.refusal_window_seconds = refusal_window_seconds


@dataclass
class _AnonBucket:
    count: int
    window_started: float


@dataclass
class _KeyBucket:
    cooldown_until: float
    failures: int
    max_failures: int
    window_started: float


@dataclass
class _Reservation:
    window_started: float
    remaining: int


def _throttled() -> DomainError:
    return KeyVerifyThrottled()


class Throttle(MeterLedger):
    """Unified throttle: anonymous IP-bucket + key-verify failure ladder.

    Both surfaces share the same CoordinationThrottleStore when bound.
    """

    MAX_FAILURES = 5
    IP_MAX_FAILURES = 5
    # The admin ladder's namespace; the per-remote scope rides the key (#1123).
    ADMIN_BUCKET = "admin"

    def __init__(
        self,
        *,
        clock=time.monotonic,
        shared_db_path: str | Path | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self._store: CoordinationThrottleStore | None = None
        self._shared_db_path: Path | None = None
        self._reservations: dict[str, _Reservation] = {}
        if shared_db_path is not None:
            self.bind_shared_db(shared_db_path)

    @property
    def shared(self) -> bool:
        return self._store is not None and self._shared_db_path is not None

    def bind_shared_db(self, db_path: str | Path) -> None:
        target = Path(db_path)
        if self._shared_db_path == target and self._store is not None:
            return
        replacement = CoordinationThrottleStore(target)
        with self._lock:
            prior = self._store
            self._store = replacement
            self._shared_db_path = target
            self._buckets.clear()
            self._reservations.clear()
        if prior is not None:
            prior.close()
        _admit_bound(self)

    def unbind_shared_db(self) -> None:
        with self._lock:
            prior = self._store
            self._store = None
            self._shared_db_path = None
            self._buckets.clear()
            self._reservations.clear()
        if prior is not None:
            prior.close()
        _drop_bound(self)

    # -- anonymous -----------------------------------------------------------

    def check_anon(self, remote: str | None) -> None:
        key = remote or _ANON_NO_CLIENT
        if self._store is None:
            self._anon_check(key)
        else:
            self._anon_check_shared(key)

    def _anon_check(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is not None:
                self._buckets.move_to_end(key)
                if now - bucket.window_started > _ANON_WINDOW:
                    bucket.count = 0
                    bucket.window_started = now
            else:
                bucket = self._anon_evict_or_mint(now)
                self._buckets[key] = bucket
            bucket.count += 1
            if bucket.count > ANON_MAX_REQUESTS:
                self._refuse_anon()

    def _anon_evict_or_mint(self, now: float) -> _AnonBucket:
        while len(self._buckets) >= _ANON_CAP:
            self._buckets.popitem(last=False)
        return _AnonBucket(count=0, window_started=now)

    def _consume_res(self, key: str, now: float) -> bool | None:
        with self._lock:
            r = self._reservations.get(key)
            if r is None or now - r.window_started > _ANON_WINDOW:
                self._reservations.pop(key, None)
                return None
            if r.remaining <= 0:
                self._reservations.pop(key, None)
                return None
            r.remaining -= 1
            if r.remaining == 0:
                self._reservations.pop(key, None)
            return True

    def _anon_check_shared(self, key: str) -> None:
        now = self._clock()
        if self._consume_res(key, now):
            return
        store = self._require_store()
        granted, ws, res = store.reserve_anonymous(
            key,
            now=now,
            window_seconds=_ANON_WINDOW,
            max_requests=ANON_MAX_REQUESTS,
        )
        self._save_reservation(key, granted, ws, res)
        if not granted:
            self._refuse_anon()

    def _refuse_anon(self) -> None:
        from crawlutron.runtime.metrics import get_metrics

        get_metrics().record_anonymous_throttle_refusal()
        raise Throttled(429, "anonymous_throttled", "Too many requests; retry later", _ANON_WINDOW)

    # -- key-verify ----------------------------------------------------------

    def check(self, presented: str, remote: str | None = None) -> None:
        self._kv_check(self._key_for(presented))
        if remote is not None:
            self._kv_check(self._ip_key_for(remote))

    def record_failure(self, presented: str, remote: str | None = None) -> None:
        self._kv_record_fail(self._key_for(presented), self.MAX_FAILURES)
        if remote is not None:
            self._kv_record_fail(self._ip_key_for(remote), self.IP_MAX_FAILURES)

    def record_success(self, presented: str, remote: str | None = None) -> None:
        self._kv_clear_sync(self._key_for(presented))

    def _kv_pop_local(self, key: str) -> bool:
        if self._store is not None:
            return False
        with self._lock:
            self._buckets.pop(key, None)
        return True

    def _kv_clear_sync(self, key: str) -> None:
        if self._store is None:
            with self._lock:
                self._buckets.pop(key, None)
        else:
            self._store.record_key_success(key)

    def _kv_check(self, key: str) -> None:
        store = self._store
        if store is not None:
            cooling, _ = store.check_key_bucket(key, now=self._clock())
            if cooling:
                self._trip_kv()
            return
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return
            self._buckets.move_to_end(key)
            now = self._clock()
        if isinstance(bucket, _KeyBucket) and bucket.cooldown_until > now:
            self._trip_kv()

    def _trip_kv(self) -> None:
        from crawlutron.runtime.metrics import get_metrics

        get_metrics().record_key_verify_throttle_trip()
        raise _throttled()

    def _kv_record_fail(self, key: str, max_failures: int) -> None:
        if self._store is not None:
            self._store.record_key_failure(
                key,
                now=self._clock(),
                max_failures=max_failures,
                failure_window_seconds=_KV_WINDOW,
                cooldown_seconds=_KV_COOLDOWN,
                bucket_capacity=_KV_CAP,
            )
            return
        with self._lock:
            now = self._clock()
            bucket = self._kv_get_or_mint(key, now, max_failures)
            if isinstance(bucket, _KeyBucket) and bucket.cooldown_until and bucket.cooldown_until <= now:
                bucket.failures = 0
                bucket.window_started = now
                bucket.cooldown_until = 0.0
            self._kv_record_into(bucket, now)

    def _kv_get_or_mint(self, key: str, now: float, max_failures: int) -> _KeyBucket:
        bucket = self._buckets.get(key)
        if isinstance(bucket, _KeyBucket):
            self._buckets.move_to_end(key)
            return bucket
        self._kv_evict_lru(now)
        bucket = _KeyBucket(cooldown_until=0.0, failures=0, max_failures=max_failures, window_started=now)
        self._buckets[key] = bucket
        return bucket

    def _kv_evict_lru(self, now: float) -> None:
        while len(self._buckets) >= _KV_CAP:
            lru = next(
                (
                    k
                    for k, b in self._buckets.items()
                    if isinstance(b, _KeyBucket) and b.cooldown_until <= now
                ),
                None,
            )
            if lru is None:
                return
            del self._buckets[lru]

    def _kv_record_into(self, bucket: _KeyBucket, now: float) -> None:
        if now - bucket.window_started > _KV_WINDOW:
            bucket.failures = 0
            bucket.window_started = now
        bucket.failures += 1
        if bucket.failures >= bucket.max_failures:
            bucket.cooldown_until = now + _KV_COOLDOWN

    # -- admin ---------------------------------------------------------------

    def check_admin(self, remote: str | None = None) -> None:
        self._kv_check(self._admin_key_for(remote))

    def record_admin_failure(self, remote: str | None = None) -> None:
        from crawlutron.runtime.metrics import get_metrics

        get_metrics().record_admin_auth_failure()
        self._kv_record_fail(self._admin_key_for(remote), self.MAX_FAILURES)

    def record_admin_success(self, remote: str | None = None) -> None:
        self._kv_clear_sync(self._admin_key_for(remote))

    # -- lifecycle -----------------------------------------------------------

    def residence(self) -> int:
        with self._lock:
            store = self._store
        if store is None:
            return len(self._buckets)
        return store.count_buckets("key_verify") + store.count_buckets("anonymous")

    def _reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._reservations.clear()
            store = self._store
        if store is not None:
            store.clear_kind("key_verify")
            store.clear_kind("anonymous")

    def _has(self) -> bool:
        with self._lock:
            if self._buckets or self._reservations:
                return True
            store = self._store
        if store is not None:
            return store.count_buckets("key_verify") > 0 or store.count_buckets("anonymous") > 0
        return False

    # -- helpers -------------------------------------------------------------

    def _key_for(self, presented: str) -> str:
        return hashlib.sha256(presented.encode("utf-8")).hexdigest()

    def _ip_key_for(self, remote: str) -> str:
        return f"ip:{remote}"

    def _admin_key_for(self, remote: str | None) -> str:
        """The admin ladder's per-remote scope; one fixed bucket when unknown."""
        return f"{self.ADMIN_BUCKET}:{remote or _ADMIN_NO_CLIENT}"

    def _require_store(self) -> CoordinationThrottleStore:
        store = self._store
        if store is None:
            raise RuntimeError("shared throttle store is not bound")
        return store

    def _save_reservation(self, key: str, granted: bool, ws: float, res: int) -> None:
        with self._lock:
            if granted and res > 1:
                self._reservations[key] = _Reservation(window_started=ws, remaining=res - 1)
            else:
                self._reservations.pop(key, None)

    # -- async twins (generated from sync) -----------------------------------

    acheck_anon = atwin(check_anon)
    acheck = atwin(check)
    arecord_failure = atwin(record_failure)
    arecord_success = atwin(record_success)
    acheck_admin = atwin(check_admin)
    arecord_admin_failure = atwin(record_admin_failure)
    arecord_admin_success = atwin(record_admin_success)
    aresidence = atwin(residence)


# Live shared-store ownership (#1123): every Throttle holding a bound store is
# admitted here, so the registry reset closes what a caller opened and forgot.
_BOUND: set[Throttle] = set()
_BOUND_LOCK = threading.RLock()
_BOUND_SHARED_DB_OWNER = "crawlutron_rest.api.throttle-shared-db"


def _admit_bound(throttle: Throttle) -> None:
    """Track one Throttle as holding a live shared store."""
    with _BOUND_LOCK:
        _BOUND.add(throttle)


def _drop_bound(throttle: Throttle) -> None:
    """Stop tracking one Throttle: its store is closed or replaced."""
    with _BOUND_LOCK:
        _BOUND.discard(throttle)


def _close_bound_stores() -> None:
    """Close every shared store still held, through the owning Throttle's own
    teardown door: a caller that opened one and forgot cannot leak it (#1123)."""
    with _BOUND_LOCK:
        bound = list(_BOUND)
        # Clear before unbinding: a Throttle admitted mid-sweep must stay owned.
        _BOUND.clear()
    for throttle in bound:
        throttle.unbind_shared_db()


def _has_bound_stores() -> bool:
    """Whether any Throttle still holds a shared store right now."""
    with _BOUND_LOCK:
        return bool(_BOUND)


_SHARED = Throttle()


def shared_throttle() -> Throttle:
    """Access the single process-wide throttle singleton."""
    return _SHARED


register_shared_meter(__name__, _SHARED)
# #1123: the shared-store connections are production-owned state: the reset
# closes every store left bound, including the app lifespan's own.
process_state.register(_BOUND_SHARED_DB_OWNER, _close_bound_stores, _has_bound_stores)

# -- anonymous middleware ----------------------------------------------------

METERED_PREFIXES = ("/docs", "/redoc")
METERED_EXACT = ("/mcp",)
_OPERATOR_METERED_EXACT = ("/metrics",)


async def throttle_anonymous_surfaces(request, call_next):
    path = request.url.path
    if await _verified(request):
        return await call_next(request)
    if path in _OPERATOR_METERED_EXACT and is_operator(request):
        return await call_next(request)
    try:
        await shared_throttle().acheck_anon(remote_host(request))
    except Throttled as t:
        return _refuse(path, t)
    return await call_next(request)


def _request_state(request) -> object | None:
    """Return request.app.state, or None when unavailable."""
    app = request.scope.get("app")
    if app is None:
        return None
    return getattr(app, "state", None)


async def _verified(request) -> bool:
    """Whether the request presents a verified key body, cheaply.
    A verified body skips the bucket; a live id with a wrong body still consumes it.
    """
    state = _request_state(request)
    if state is None:
        return False
    store = getattr(state, "caller_key_store", None)
    if store is None:
        return False
    return await store.akey_body_proven(presented_key(request) or "")


def _refuse(path: str, t: Throttled) -> JSONResponse:
    from crawlutron_mcp.constants import RATE_LIMITED
    from crawlutron_mcp.protocol import error_response

    from crawlutron_rest.api.exceptions import error_envelope

    if path in METERED_EXACT:
        content: object = error_response(None, RATE_LIMITED, t.refusal_message)
    else:
        content = error_envelope(t.refusal_code, t.refusal_message)
    return JSONResponse(
        status_code=t.refusal_status,
        headers={"Retry-After": str(int(t.refusal_window_seconds))},
        content=content,
    )
