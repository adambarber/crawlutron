"""Shared throttle bucket storage for cross-worker pre-auth meters (#941)."""

from __future__ import annotations

from crawlutron.storage.coordination_store import CoordinationStore, atwin
from crawlutron.storage.migrations.manifest import StoreSchema
from crawlutron.storage.safe_db import (
    assert_data_version_stable,
    in_txn,
    read_data_version,
    txn_read,
    txn_write,
)

__all__ = ["CoordinationThrottleStore"]

_ANON_BATCH = 8
_KEY_VERIFY_KIND = "key_verify"
_ANON_KIND = "anonymous"


class CoordinationThrottleStore(CoordinationStore):
    """SQLite-backed throttle buckets shared by every worker process."""

    schema = StoreSchema.COORDINATION_THROTTLE_BUCKETS

    def reserve_anonymous(
        self,
        bucket_key: str,
        *,
        now: float,
        window_seconds: float,
        max_requests: int,
        batch: int = _ANON_BATCH,
        capacity: int = 1024,
    ) -> tuple[bool, float, int]:
        """Reserve up to `batch` units for `bucket_key` in the current window."""
        want = max(1, int(batch))
        with self._lock:
            with txn_write(self._conn, owner="coordination_throttle_store.reserve_anonymous"):
                window_started, count = self._anon_window_state(bucket_key, now, window_seconds, capacity)
                granted = self._apply_anon_reservation(
                    bucket_key, now, window_started, count, want, max_requests
                )
                return granted

    def _anon_window_state(
        self, bucket_key: str, now: float, window_seconds: float, capacity: int
    ) -> tuple[float, int]:
        row = self._conn.execute(
            "SELECT window_started, count FROM coordination_throttle_buckets "
            "WHERE kind = ? AND bucket_key = ?",
            (_ANON_KIND, bucket_key),
        ).fetchone()
        if row is None:
            self._evict_anon_buckets(now, window_seconds, capacity)
            return now, 0
        window_started = float(row[0])
        if now - window_started > window_seconds:
            return now, 0
        return window_started, int(row[1])

    def _apply_anon_reservation(
        self,
        bucket_key: str,
        now: float,
        window_started: float,
        count: int,
        want: int,
        max_requests: int,
    ) -> tuple[bool, float, int]:
        if count >= max_requests:
            self._upsert_anon(bucket_key, window_started, count, now)
            return False, window_started, 0
        grant = min(want, max_requests - count)
        self._upsert_anon(bucket_key, window_started, count + grant, now)
        return True, window_started, grant

    def _upsert_anon(self, bucket_key: str, window_started: float, count: int, now: float) -> None:
        self._upsert(
            _ANON_KIND,
            bucket_key,
            window_started=window_started,
            count=count,
            failures=0,
            cooldown_until=0.0,
            max_failures=0,
            touched_at=now,
        )

    def _evict_anon_buckets(self, now: float, window_seconds: float, capacity: int) -> None:
        count_row = self._conn.execute(
            "SELECT COUNT(*) FROM coordination_throttle_buckets WHERE kind = ?",
            (_ANON_KIND,),
        ).fetchone()
        count = 0 if count_row is None else int(count_row[0])
        if count < capacity:
            return
        # Prefer evicting expired/quiescent buckets; never evict most-recently active if avoidable.
        victim = self._conn.execute(
            "SELECT bucket_key FROM coordination_throttle_buckets "
            "WHERE kind = ? ORDER BY touched_at ASC LIMIT 1",
            (_ANON_KIND,),
        ).fetchone()
        if victim is not None:
            with in_txn(self._conn):
                self._conn.execute(
                    "DELETE FROM coordination_throttle_buckets WHERE kind = ? AND bucket_key = ?",
                    (_ANON_KIND, str(victim[0])),
                )

    def check_key_bucket(
        self,
        bucket_key: str,
        *,
        now: float,
    ) -> tuple[bool, float]:
        """Return (cooling, cooldown_until) for a key-verify bucket."""
        with self._lock:
            version = read_data_version(self._conn)
            with txn_read(
                self._conn,
                max_rows=1,
                owner="coordination_throttle_store.check_key_bucket",
            ):
                row = self._conn.execute(
                    "SELECT cooldown_until FROM coordination_throttle_buckets "
                    "WHERE kind = ? AND bucket_key = ?",
                    (_KEY_VERIFY_KIND, bucket_key),
                ).fetchone()
            assert_data_version_stable(self._conn, version)
            if row is None:
                return False, 0.0
            cooldown_until = float(row[0] or 0.0)
            with txn_write(self._conn, owner="coordination_throttle_store.check_key_bucket"):
                self._conn.execute(
                    "UPDATE coordination_throttle_buckets SET touched_at = ? "
                    "WHERE kind = ? AND bucket_key = ?",
                    (now, _KEY_VERIFY_KIND, bucket_key),
                )
            return cooldown_until > now, cooldown_until

    def record_key_failure(
        self,
        bucket_key: str,
        *,
        now: float,
        max_failures: int,
        failure_window_seconds: float,
        cooldown_seconds: float,
        bucket_capacity: int,
    ) -> None:
        """Record one failure and arm cooldown when threshold reached."""
        with self._lock:
            with txn_write(self._conn, owner="coordination_throttle_store.record_key_failure"):
                self._apply_key_failure(
                    bucket_key, now, max_failures, failure_window_seconds, cooldown_seconds, bucket_capacity
                )

    def _apply_key_failure(
        self,
        bucket_key: str,
        now: float,
        max_failures: int,
        failure_window_seconds: float,
        cooldown_seconds: float,
        bucket_capacity: int,
    ) -> None:
        self._evict_key_buckets(now, bucket_capacity)
        failures, window_started, cooldown_until = self._key_failure_state(bucket_key, now)
        if now - window_started > failure_window_seconds:
            failures = 0
            window_started = now
        failures += 1
        if failures >= max_failures:
            cooldown_until = now + cooldown_seconds
        self._upsert(
            _KEY_VERIFY_KIND,
            bucket_key,
            window_started=window_started,
            count=0,
            failures=failures,
            cooldown_until=cooldown_until,
            max_failures=max_failures,
            touched_at=now,
        )

    def _key_failure_state(self, bucket_key: str, now: float) -> tuple[int, float, float]:
        row = self._conn.execute(
            "SELECT failures, window_started, cooldown_until FROM coordination_throttle_buckets "
            "WHERE kind = ? AND bucket_key = ?",
            (_KEY_VERIFY_KIND, bucket_key),
        ).fetchone()
        if row is None:
            return 0, now, 0.0
        failures = int(row[0] or 0)
        window_started = float(row[1] or now)
        cooldown_until = float(row[2] or 0.0)
        if cooldown_until and cooldown_until <= now:
            return 0, now, 0.0
        return failures, window_started, cooldown_until

    def record_key_success(self, bucket_key: str) -> None:
        """Clear one key-verify bucket (success path)."""
        with self._lock:
            with txn_write(self._conn, owner="coordination_throttle_store.record_key_success"):
                self._conn.execute(
                    "DELETE FROM coordination_throttle_buckets WHERE kind = ? AND bucket_key = ?",
                    (_KEY_VERIFY_KIND, bucket_key),
                )

    def count_buckets(self, kind: str) -> int:
        with self._lock:
            with txn_read(
                self._conn,
                max_rows=1,
                owner="coordination_throttle_store.count_buckets",
            ):
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM coordination_throttle_buckets WHERE kind = ?",
                    (kind,),
                ).fetchone()
        return 0 if row is None else int(row[0])

    def clear_kind(self, kind: str) -> None:
        with self._lock:
            with txn_write(self._conn, owner="coordination_throttle_store.clear_kind"):
                self._conn.execute(
                    "DELETE FROM coordination_throttle_buckets WHERE kind = ?",
                    (kind,),
                )

    def clear_all(self) -> None:
        with self._lock:
            with txn_write(self._conn, owner="coordination_throttle_store.clear_all"):
                self._conn.execute("DELETE FROM coordination_throttle_buckets")

    areserve_anonymous = atwin(reserve_anonymous)
    acheck_key_bucket = atwin(check_key_bucket)
    arecord_key_failure = atwin(record_key_failure)
    arecord_key_success = atwin(record_key_success)

    def _evict_key_buckets(self, now: float, capacity: int) -> None:
        count_row = self._conn.execute(
            "SELECT COUNT(*) FROM coordination_throttle_buckets WHERE kind = ?",
            (_KEY_VERIFY_KIND,),
        ).fetchone()
        count = 0 if count_row is None else int(count_row[0])
        if count < capacity:
            return
        # Evict least-recently-touched non-cooling buckets first; cooling exempt.
        while count >= capacity:
            victim = self._conn.execute(
                "SELECT bucket_key FROM coordination_throttle_buckets "
                "WHERE kind = ? AND (cooldown_until IS NULL OR cooldown_until <= ?) "
                "ORDER BY touched_at ASC LIMIT 1",
                (_KEY_VERIFY_KIND, now),
            ).fetchone()
            if victim is None:
                return
            with in_txn(self._conn):
                self._conn.execute(
                    "DELETE FROM coordination_throttle_buckets WHERE kind = ? AND bucket_key = ?",
                    (_KEY_VERIFY_KIND, str(victim[0])),
                )
            count -= 1

    def _upsert(
        self,
        kind: str,
        bucket_key: str,
        *,
        window_started: float,
        count: int,
        failures: int,
        cooldown_until: float,
        max_failures: int,
        touched_at: float,
    ) -> None:
        with in_txn(self._conn):
            self._conn.execute(
                "INSERT INTO coordination_throttle_buckets "
                "(kind, bucket_key, window_started, count, failures, "
                "cooldown_until, max_failures, touched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, bucket_key) DO UPDATE SET "
                "window_started = excluded.window_started, count = excluded.count, "
                "failures = excluded.failures, cooldown_until = excluded.cooldown_until, "
                "max_failures = excluded.max_failures, touched_at = excluded.touched_at",
                (kind, bucket_key, window_started, count, failures, cooldown_until, max_failures, touched_at),
            )
