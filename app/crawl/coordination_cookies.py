"""Cross-process shared cookie storage.

`CookieJar` kept cookies in a per-process dict and persisted them by
serialising the whole jar over a JSON file. The atomic-write chokepoint makes
that write *non-truncated*; it does nothing about a *lost update*, because the
update is replace-everything. Each worker loads at startup and saves at
shutdown, so with N workers shutdown is last-writer-wins: the last process to
exit discards every cookie the others collected and the deployment still
reports healthy.

A shared table makes the write additive instead. Two workers crawling one host
set different cookies, so `merge` reads the row and writes back the union
inside one transaction -- the read-modify-write is what the file could not do,
because between its read and its replace another process had nothing to lose.

`touched_at` carries the LRU clock. The jar is long-lived and every crawled
host contributes a slot, so the table needs the same bound the in-memory map
had, and eviction has to happen here because the map is no longer the thing
being bounded.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.crawl.coordination_store import CoordinationStore
from app.crawl.migrations.manifest import StoreSchema

__all__ = ["DEFAULT_MAX_HOSTS", "SharedCookieStore"]

# Cap on distinct (namespace, host) slots. The jar is long-lived: without
# eviction every crawled host accumulates forever. The DNS, robots and sitemap
# caches are all bounded for the same reason.
DEFAULT_MAX_HOSTS = 1024


class SharedCookieStore(CoordinationStore):
    """Cookie storage shared by every worker process.

    ``namespace`` and ``host`` are both caller-derived, so both are bound
    parameters on every statement; no path here interpolates either into SQL.
    """

    schema = StoreSchema.COORDINATION_COOKIES

    def __init__(self, db_path: str | Path, *, max_hosts: int = DEFAULT_MAX_HOSTS) -> None:
        super().__init__(db_path)
        self._max_hosts = max(1, int(max_hosts))

    def get(self, namespace: str, host: str) -> dict[str, str]:
        """Return the cookies for one slot and mark it recently used."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM coordination_cookies "
                "WHERE namespace = ? AND host = ?",
                (namespace or "", host),
            ).fetchone()
            if row is None:
                return {}
            # A read refreshes recency, so a host that is being crawled
            # survives the next eviction instead of the oldest-unread slot.
            self._conn.execute(
                "UPDATE coordination_cookies SET touched_at = ? "
                "WHERE namespace = ? AND host = ?",
                (now, namespace or "", host),
            )
            self._conn.commit()
        return self._decode(row[0])

    def merge(self, namespace: str, host: str, cookies: dict[str, str]) -> None:
        """Union ``cookies`` into the slot, then evict past the cap.

        The read and the write are one transaction, which is the whole point:
        the JSON file's replace-everything write is what lost a sibling's
        cookies, and only an in-transaction union avoids it.
        """
        now = time.time()
        key = (namespace or "", host)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT payload FROM coordination_cookies "
                    "WHERE namespace = ? AND host = ?",
                    key,
                ).fetchone()
                merged = {**self._decode(row[0]), **cookies} if row else dict(cookies)
                self._conn.execute(
                    "INSERT INTO coordination_cookies (namespace, host, payload, touched_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(namespace, host) DO UPDATE SET "
                    "payload = excluded.payload, touched_at = excluded.touched_at",
                    (key[0], key[1], json.dumps(merged), now),
                )
                self._conn.execute(
                    "DELETE FROM coordination_cookies WHERE (namespace, host) IN ("
                    "SELECT namespace, host FROM coordination_cookies "
                    "ORDER BY touched_at ASC LIMIT -1 OFFSET ?)",
                    (self._max_hosts,),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def clear(self, namespace: str = "") -> None:
        """Clear one namespace, or every slot when ``namespace`` is empty."""
        with self._lock:
            if namespace:
                self._conn.execute(
                    "DELETE FROM coordination_cookies WHERE namespace = ?", (namespace,)
                )
            else:
                self._conn.execute("DELETE FROM coordination_cookies")
            self._conn.commit()

    def all_slots(self) -> dict[tuple[str, str], dict[str, str]]:
        """Every stored slot, for the import/export path."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT namespace, host, payload FROM coordination_cookies"
            ).fetchall()
        return {
            (str(namespace), str(host)): self._decode(payload)
            for namespace, host, payload in rows
        }

    @staticmethod
    def _decode(payload: str) -> dict[str, str]:
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            # A row this process did not write and cannot parse is treated as
            # empty rather than crashing every request that reads the host.
            return {}
        return {str(k): str(v) for k, v in decoded.items()} if isinstance(decoded, dict) else {}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
