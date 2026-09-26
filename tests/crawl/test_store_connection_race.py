"""Connection setup must wait out a peer that is using the database.

`configure_store_connection` sets `PRAGMA journal_mode=WAL` on every connection
it opens. WAL is a persistent property of the *database file*, not a
per-connection setting, so re-issuing the change is unnecessary once set -- and
on the first conversion, when the file is still in `delete` mode, it is a write
that SQLite refuses with SQLITE_BUSY rather than waiting. `busy_timeout` does
not cover a journal-mode change, so the busy handler is never invoked.

Proven with four spawned processes driving the real store, three runs: two clean
at 800/800, one where three of four children died at sqlite_store.py:39 with
"database is locked" and the increments they were responsible for never landed.
`uvicorn --workers` starts its children together, so on a fresh database every
one of them races the same first conversion. Every store in the service opens
through this chokepoint.

A race that fires one run in three makes a bad gate, so these tests hold a lock
that is *proven* held before the conversion is attempted, and assert the two
behaviours that matter: a peer that releases must be waited out, and a peer
that never releases must still be refused rather than hung on forever.
"""

import sqlite3
import threading
import time

from app.crawl import sqlite_store
from app.crawl.sqlite_store import configure_store_connection


def _delete_mode_database(path: str) -> None:
    """A fresh database is in `delete` journal mode, so the conversion is a write."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "delete"


class _PeerHoldingTheFile:
    """A connection holding an exclusive lock until the test releases it.

    The lock is taken on a thread but proven taken via an event before the
    caller proceeds, so the test cannot race past a peer that has not started.
    SQLite's locking is file-level, so a second connection in this process is
    exactly what a sibling worker process is to the converting connection.
    """

    def __init__(self, db_path: str) -> None:
        self._holding = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(
            target=self._hold, args=(db_path,), daemon=True
        )
        self._thread.start()
        assert self._holding.wait(10), "peer never acquired the lock"

    def _hold(self, db_path: str) -> None:
        peer = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        peer.execute("BEGIN EXCLUSIVE")
        self._holding.set()
        self._release.wait(30)
        peer.execute("ROLLBACK")
        peer.close()

    def release(self) -> None:
        self._release.set()
        self._thread.join(10)


def test_setup_outlasts_a_peer_beyond_the_busy_timeout(tmp_path, monkeypatch):
    """Proves the retry loop, not `busy_timeout`, is what carries the wait.

    The busy timeout is cut to 50ms so a 0.6s peer cannot be outwaited by it.
    `busy_timeout` was measured never firing for this statement at all -- the
    failing children died 0.000s in -- so with the timeout this small, success
    can only come from the retry.
    """
    monkeypatch.setattr(sqlite_store, "_BUSY_TIMEOUT_MS", 50)
    db_path = str(tmp_path / "retry.db")
    _delete_mode_database(db_path)

    peer = _PeerHoldingTheFile(db_path)
    threading.Timer(0.6, peer.release).start()

    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    try:
        configure_store_connection(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_setup_refuses_a_peer_that_never_releases(tmp_path, monkeypatch):
    """Bounded, not forever: a hard lock must not become an unkillable hang."""
    monkeypatch.setattr(sqlite_store, "_WAL_RETRY_SECONDS", 0.5)
    db_path = str(tmp_path / "stuck.db")
    _delete_mode_database(db_path)

    peer = _PeerHoldingTheFile(db_path)
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        started = time.monotonic()
        try:
            configure_store_connection(conn)
        except sqlite3.OperationalError:
            waited = time.monotonic() - started
            assert waited < 10, f"waited {waited:.1f}s, which is not bounded"
        else:
            raise AssertionError("expected a refusal while a peer held the database")
        finally:
            conn.close()
    finally:
        peer.release()


def test_already_wal_database_is_never_written(tmp_path):
    """Once the property is set, setup must not touch the file at all.

    An exclusive lock makes any write fail, so surviving it is the proof.
    """
    db_path = str(tmp_path / "wal.db")
    _delete_mode_database(db_path)
    seed = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.close()

    peer = _PeerHoldingTheFile(db_path)
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        configure_store_connection(conn)
        conn.close()
    finally:
        peer.release()
