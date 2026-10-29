"""The ``watch_store`` fixture: a per-test SQLite watch store."""

import pytest


@pytest.fixture
def watch_store(tmp_path):
    """WatchStore backed by a per-test SQLite db under ``tmp_path``."""
    from crawlutron.storage.watch_store import WatchStore

    return WatchStore(tmp_path / "watches.db")
