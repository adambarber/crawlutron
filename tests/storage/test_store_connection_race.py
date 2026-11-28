"""Connection setup must wait out a peer that is using the database.

A first ``PRAGMA journal_mode=WAL`` conversion is a write SQLite refuses with
``SQLITE_BUSY``, so these tests hold a proven lock and assert wait-out vs refusal.
"""

import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest

from crawlutron.storage import sqlite_store
from crawlutron.storage.sqlite_store import configure_store_connection
from tests._clocks import Timeline


def _delete_mode_database(path: str) -> None:
    """A fresh database is in `delete` journal mode, so the conversion is a write."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    probe = sqlite3.connect(path)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        probe.close()


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


def test_setup_outlasts_a_peer_beyond_the_busy_timeout(tmp_path):
    """Proves the retry loop, not `busy_timeout`, is what carries the wait.

    The timeout is cut to 50ms so a 0.6s peer cannot be outwaited by it -- it
    never fired for this statement, so success can only come from the retry.
    """
    with _peer_locked_store(tmp_path, "retry", release_after=0.6) as conn:
        configure_store_connection(conn, busy_timeout_ms=50)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_setup_refuses_a_peer_that_never_releases(monkeypatch) -> None:
    """A spent deadline re-raises at once: no backoff sleep, no second try."""
    timeline = Timeline(1000.0)
    sleeps: list[float] = []

    class _LockedConnection:
        isolation_level = None

        def __init__(self) -> None:
            self.timeouts: list[int] = []
            self.attempts = 0
            self.progress_handler: Callable[[], int] | None = None

        def set_progress_handler(self, handler, n: int) -> None:
            self.progress_handler = handler

        def execute(self, statement: str):
            if statement.startswith("PRAGMA busy_timeout="):
                self.timeouts.append(int(statement.rsplit("=", 1)[1]))
                return self
            self.attempts += 1
            assert self.attempts == 1, "the retry loop kept going past its deadline"
            timeline.advance(0.5)
            raise sqlite3.OperationalError("database is locked")

    # The chokepoint only runs PRAGMAs here; this stand-in refuses the WAL one without a real lock.
    stub = _LockedConnection()
    conn = cast(sqlite3.Connection, stub)
    monkeypatch.setattr(sqlite_store, "time", SimpleNamespace(monotonic=timeline, sleep=sleeps.append))

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        configure_store_connection(conn, busy_timeout_ms=5000, wal_retry_seconds=0.5)

    assert sleeps == [], "a spent deadline must not buy another backoff sleep"
    assert stub.timeouts == [5000, 500, 5000], stub.timeouts


def test_stalled_wal_conversion_refused_within_budget(monkeypatch) -> None:
    """A stalled WAL conversion is refused within the wal_retry_seconds budget."""
    timeline = Timeline(1000.0)
    sleeps: list[float] = []
    wal_retry_seconds = 0.5
    quantum = 0.005  # one progress-handler tick may advance the clock past the deadline

    class _StalledConnection:
        isolation_level = None

        def __init__(self) -> None:
            self.timeouts: list[int] = []
            self.progress_handler: Callable[[], int] | None = None

        def set_progress_handler(self, handler, n: int) -> None:
            self.progress_handler = handler

        def execute(self, statement: str):
            if statement.startswith("PRAGMA busy_timeout="):
                self.timeouts.append(int(statement.rsplit("=", 1)[1]))
                return self
            # Simulate a busy wait that ignores busy_timeout; it yields only when
            # the progress handler aborts at the deadline.
            if self.progress_handler is None:
                timeline.advance(10.0)
            else:
                while self.progress_handler() == 0:
                    timeline.advance(0.005)
            raise sqlite3.OperationalError("database is locked")

    stub = _StalledConnection()
    conn = cast(sqlite3.Connection, stub)
    monkeypatch.setattr(sqlite_store, "time", SimpleNamespace(monotonic=timeline, sleep=sleeps.append))

    started = timeline()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        configure_store_connection(conn, busy_timeout_ms=5000, wal_retry_seconds=0.5)

    assert timeline() - started <= wal_retry_seconds + quantum, f"overshot budget: {timeline() - started}s"
    assert sleeps == [], "no backoff sleep past the deadline"


def test_already_wal_database_is_never_written(tmp_path):
    """Once the property is set, setup must not touch the file at all.

    An exclusive lock makes any write fail, so surviving it is the proof.
    """
    with _peer_locked_store(tmp_path, "wal", already_wal=True) as conn:
        configure_store_connection(conn)
