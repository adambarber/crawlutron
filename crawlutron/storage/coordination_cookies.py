"""Cross-process shared cookie storage.

A shared table beats the old replace-everything file (last-writer-wins lost updates):
``merge`` writes back the union in one transaction; ``touched_at`` carries the LRU clock for eviction.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path

from crawlutron.storage.coordination_store import CoordinationStore
from crawlutron.storage.migrations.manifest import StoreSchema

__all__ = ["DEFAULT_MAX_HOSTS_PER_NAMESPACE", "SharedCookieStore"]

# The DNS, robots and sitemap caches are all bounded for the same reason: an unbounded
# eviction accumulates every host forever.
DEFAULT_MAX_HOSTS_PER_NAMESPACE = 1024


class SharedCookieStore(CoordinationStore):
    """Cookie storage shared by every worker process.

    ``namespace`` and ``host`` are both caller-derived, so both are bound
    parameters on every statement; no path here interpolates either into SQL.
    """

    schema = StoreSchema.COORDINATION_COOKIES

    def __init__(self, db_path: str | Path, *, max_hosts: int = DEFAULT_MAX_HOSTS_PER_NAMESPACE) -> None:
        super().__init__(db_path)
        self._max_hosts = max(1, int(max_hosts))

    def load(self, namespace: str, host: str) -> dict[str, str]:
        """Return the cookies for one slot.

        Read-only: a SELECT and a decode, no write transaction. Recency is
        refreshed through the explicit ``touch`` door (the jar calls it for
        contributor hosts), so a read never generates WAL frames or holds the
        SQLite write lock.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM coordination_cookies WHERE namespace = ? AND host = ?",
                (namespace or "", host),
            ).fetchone()
        if row is None:
            return {}
        return self._decode(row[0])

    def merge(self, namespace: str, host: str, cookies: Mapping[str, str]) -> None:
        """Union ``cookies`` into the slot, then evict past the per-namespace cap.
        Eviction scopes to the slot being written, so one tenant's crawl cannot
        evict another tenant's sessions. The union stays transactional.

        Lock order: SQLite write lock (``BEGIN IMMEDIATE``) first, then the
        process store lock -- never the reverse. That single order keeps
        ``merge``/``put``/``touch``/``clear`` from deadlocking on one another
        when a cross-process writer holds the write lock.
        """
        key = (namespace or "", host)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload FROM coordination_cookies WHERE namespace = ? AND host = ?",
                    key,
                ).fetchone()
                merged = {**self._decode(row[0]), **cookies} if row else dict(cookies)
                self._upsert_slot(key[0], key[1], json.dumps(merged), time.time())
                self._conn.execute(
                    "DELETE FROM coordination_cookies WHERE (namespace, host) IN ("
                    "SELECT namespace, host FROM coordination_cookies WHERE namespace = ? "
                    "ORDER BY touched_at ASC LIMIT -1 OFFSET ?)",
                    (key[0], self._max_hosts),
                )
                self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def clear(self, namespace: str = "") -> None:
        """Clear one namespace, or every slot when ``namespace`` is empty."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            with self._lock:
                if namespace:
                    self._conn.execute("DELETE FROM coordination_cookies WHERE namespace = ?", (namespace,))
                else:
                    self._conn.execute("DELETE FROM coordination_cookies")
                self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def touch(self, namespace: str, host: str) -> None:
        """Mark one slot recently used (the read path)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE coordination_cookies SET touched_at = ? WHERE namespace = ? AND host = ?",
                    (time.time(), namespace or "", host),
                )
                self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def slots_for_namespace(self, namespace: str) -> dict[str, dict[str, str]]:
        """One namespace's slots as {host: cookies}; the WHERE is what keeps a
        per-request read from deserializing every other tenant's payloads."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT host, payload FROM coordination_cookies WHERE namespace = ?",
                (namespace or "",),
            ).fetchall()
        return {str(host): self._decode(payload) for host, payload in rows}

    def put(self, namespace: str, host: str, value: Mapping[str, str]) -> None:
        """Replace one slot's payload wholesale (the prune path).

        Scoped to exactly this (namespace, host); only removes records, so a
        prune cannot resurrect a deleted cookie.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            with self._lock:
                self._upsert_slot(namespace or "", host, json.dumps(dict(value)), time.time())
                self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _decode(payload: str) -> dict[str, str]:
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            # A row this process did not write and cannot parse is treated as
            # empty rather than crashing every request that reads the host.
            return {}
        return {str(k): str(v) for k, v in decoded.items()} if isinstance(decoded, dict) else {}

    def _upsert_slot(self, namespace: str, host: str, payload: str, touched_at: float) -> None:
        """The one upsert statement; ``merge`` and ``put`` differ only in their caller."""
        self._conn.execute(
            "INSERT INTO coordination_cookies (namespace, host, payload, touched_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(namespace, host) DO UPDATE SET "
            "payload = excluded.payload, touched_at = excluded.touched_at",
            (namespace, host, payload, touched_at),
        )
