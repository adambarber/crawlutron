"""Connection setup must wait out a peer that is using the database.

A first ``PRAGMA journal_mode=WAL`` conversion is a write SQLite refuses with
``SQLITE_BUSY``, so these tests hold a proven lock and assert wait-out vs refusal.
"""

import sqlite3
import threading
import time
from contextlib import contextmanager

from app.storage import sqlite_store
from app.storage.sqlite_store import configure_store_connection


def _delete_mode_database(path: str) -> None:
    """A fresh database is in `delete` journal mode, so the conversion is a write."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "delete"


class _PeerHoldingTheFile:
    """A connection holding an exclusive lock until the test releases it.

    The lock is proven taken via an event, so the test cannot race past a peer
    that has not started; a file-level lock makes this a sibling worker.
    """

    def __init__(self, db_path: str) -> None:
        self._holding = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._hold, args=(db_path,), daemon=True)
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


@contextmanager
def _peer_locked_store(tmp_path, name: str, *, already_wal: bool = False, release_after: float | None = None):
    """A store database with a peer PROVEN holding it, and our connection.

    Yields the connection the chokepoint runs through and cleans up on the way
    out; ``release_after`` = waited-out, None = refused, ``already_wal`` converts.
    """
    db_path = str(tmp_path / f"{name}.db")
    _delete_mode_database(db_path)
    if already_wal:
        seed = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        seed.execute("PRAGMA journal_mode=WAL")
        seed.close()

    peer = _PeerHoldingTheFile(db_path)
    timer = None
    if release_after is not None:
        timer = threading.Timer(release_after, peer.release)
        timer.start()
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()
        if timer is not None:
            timer.cancel()
        peer.release()


def test_setup_outlasts_a_peer_beyond_the_busy_timeout(tmp_path, monkeypatch):
    """Proves the retry loop, not `busy_timeout`, is what carries the wait.

    The timeout is cut to 50ms so a 0.6s peer cannot be outwaited by it -- it
    never fired for this statement, so success can only come from the retry.
    """
    monkeypatch.setattr(sqlite_store, "_BUSY_TIMEOUT_MS", 50)

    with _peer_locked_store(tmp_path, "retry", release_after=0.6) as conn:
        configure_store_connection(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_setup_refuses_a_peer_that_never_releases(tmp_path, monkeypatch):
    """Bounded, not forever: a hard lock must not become an unkillable hang.

    The bound is the caller's, asserted tightly; a loose "under ten seconds"
    once let a budget checked only between attempts pass.
    """
    monkeypatch.setattr(sqlite_store, "_WAL_RETRY_SECONDS", 0.5)

    with _peer_locked_store(tmp_path, "stuck") as conn:
        started = time.monotonic()
        try:
            configure_store_connection(conn)
        except sqlite3.OperationalError:
            waited = time.monotonic() - started
            assert waited < 0.75, f"waited {waited:.2f}s against a 0.5s budget"
        else:
            raise AssertionError("expected a refusal while a peer held the database")


def test_already_wal_database_is_never_written(tmp_path):
    """Once the property is set, setup must not touch the file at all.

    An exclusive lock makes any write fail, so surviving it is the proof.
    """
    with _peer_locked_store(tmp_path, "wal", already_wal=True) as conn:
        configure_store_connection(conn)
