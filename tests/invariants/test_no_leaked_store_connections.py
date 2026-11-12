"""No sqlite3.Connection opened during a test may outlive the session unclosed. Leaked store connections are
the unraisable-ResourceWarning flake family under Python 3.14 finalization: GC closes them at a
nondeterministic moment, so a different invariant test reds each run.
"""

from __future__ import annotations

import gc
import sqlite3
import threading
import traceback

import pytest

_LEDGER_LOCK = threading.Lock()
_LEDGER: dict[int, sqlite3.Connection] = {}  # per-test: strong refs, anti-finalize
_SESSION_LEDGER: dict[int, sqlite3.Connection] = {}  # whole session: the sweep's input
_SITES: dict[int, str] = {}  # connection id -> open site (attribution)
_CURRENT_TEST: str = "?"  # the running test's nodeid, set by the conftest fixture
_REAL_CONNECT = sqlite3.connect


def _open_site() -> str:
    stack = traceback.extract_stack()[:-1]
    prod = [
        f"{f.filename.split('/crawlutron/')[-1]}:{f.lineno}"
        for f in reversed(stack)
        if "/crawlutron/" in f.filename and "test_no_leaked_store_connections" not in f.filename
    ]
    caller = next(
        (
            f"{f.filename.split('/tests/')[-1]}:{f.lineno} {f.name}"
            for f in reversed(stack)
            if "/tests/" in f.filename and "test_no_leaked_store_connections" not in f.filename
        ),
        "?",
    )
    return f"{prod[0] if prod else '?'} <- {caller} [{_CURRENT_TEST}]"


def _tracking_connect(*args, **kwargs):
    conn = _REAL_CONNECT(*args, **kwargs)
    site = _open_site()
    with _LEDGER_LOCK:
        _LEDGER[id(conn)] = conn
        _SESSION_LEDGER[id(conn)] = conn
        _SITES[id(conn)] = site
    return conn


def live_ledgered_sites() -> list[str]:
    """Open-site attribution of every session-tracked connection still executing.

    ``gc.collect()`` first: finalize-registered stores close exactly here, so
    what remains live after collection is a real leak, not pending cleanup."""
    gc.collect()
    with _LEDGER_LOCK:
        tracked = dict(_SESSION_LEDGER)
    alive: list[str] = []
    wanted = set(tracked)
    for obj in gc.get_objects():
        if id(obj) in wanted and isinstance(obj, sqlite3.Connection):
            try:
                obj.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                continue
            alive.append(_SITES.get(id(obj), "?"))
    return alive


@pytest.fixture(autouse=True)
def _every_store_connection_a_test_opens_it_closes(monkeypatch, request):
    """Track every sqlite3.connect this test module performs (self-coverage only)."""
    global _CURRENT_TEST

    with _LEDGER_LOCK:
        _LEDGER.clear()
    monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
    _CURRENT_TEST = request.node.nodeid
    yield
    _CURRENT_TEST = "?"
    with _LEDGER_LOCK:
        _LEDGER.clear()


def test_the_ledger_catches_a_leaked_connection(tmp_path):
    """A connection opened and never closed is named by the session sweep."""
    conn = _REAL_CONNECT(tmp_path / "leak.db")
    try:
        with _LEDGER_LOCK:
            _SESSION_LEDGER[id(conn)] = conn
            _SITES[id(conn)] = "the_leak_test"
        with pytest.raises(AssertionError, match="the_leak_test"):
            from tests.conftest import assert_no_live_session_connections

            assert_no_live_session_connections()
    finally:
        conn.close()
        with _LEDGER_LOCK:
            _SESSION_LEDGER.pop(id(conn), None)
            _SITES.pop(id(conn), None)


__all__ = ["live_ledgered_sites", "test_the_ledger_catches_a_leaked_connection"]
