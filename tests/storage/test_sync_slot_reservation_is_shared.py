"""A sync slot reserved by one store blocks the next store on the same DB."""

from __future__ import annotations

from pathlib import Path

from crawlutron.storage.coordination_sync_slots import CoordinationSyncSlotsStore


def test_second_store_is_refused_after_first_reserves(tmp_path: Path) -> None:
    """Two store instances over one DB cannot both hold the single slot."""
    db = tmp_path / "coord.db"
    first = CoordinationSyncSlotsStore(db)
    second = CoordinationSyncSlotsStore(db)
    try:
        assert first.reserve("test-caller", 1) is not None
        assert second.reserve("test-caller", 1) is None
    finally:
        first.close()
        second.close()
