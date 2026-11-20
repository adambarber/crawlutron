"""The ``watch_store`` fixture: a per-test SQLite watch store."""

from collections.abc import Generator

import pytest

from crawlutron.storage.safe_db import allow_on_loop


@pytest.fixture
def watch_store(tmp_path) -> Generator[object]:
    """WatchStore backed by a per-test SQLite db under ``tmp_path``.

    Async tests drive sync writes on the loop; ``allow_on_loop`` is the
    sanctioned door that permits that (writes still run through ``txn_write``).
    """
    from crawlutron.storage.watch_store import WatchStore

    store = WatchStore(tmp_path / "watches.db")
    with allow_on_loop():
        yield store
    store.close()
