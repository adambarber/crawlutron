"""Cross-worker sync-crawl slot sharing: the quota must not multiply at W>1."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.errors import QuotaRefusal
from crawlutron.storage.coordination_sync_slots import CoordinationSyncSlotsStore


def _tracker(db_path: Path) -> CallerConcurrencyTracker:
    """Return a tracker backed by the same coordination DB."""
    return CallerConcurrencyTracker(coordination_db_path=db_path)


def _reserve_and_refuse(
    db: Path, caller: str, max_conc: int
) -> tuple[CallerConcurrencyTracker, CallerConcurrencyTracker]:
    """Reserve once then verify a second tracker is refused."""
    a = _tracker(db)
    b = _tracker(db)
    a.reserve(caller, max_concurrency=max_conc)
    with pytest.raises(QuotaRefusal):
        b.reserve(caller, max_concurrency=max_conc)
    b.close()
    return a, b


def test_two_trackers_share_one_slot(tmp_path: Path) -> None:
    """Two tracker instances bound to the same DB: W=2 with max_concurrency=1
    must refuse the second admit."""
    db = tmp_path / "crawler.db"
    a, b = _reserve_and_refuse(db, "ws-tenant", 1)
    a.close()


def test_release_allows_spare_reserve(tmp_path: Path) -> None:
    """After releasing, the shared count falls and a spare can reserve."""
    db = tmp_path / "crawler.db"
    a, _ = _reserve_and_refuse(db, "ws-tenant", 1)
    a.release("ws-tenant")
    a.close()
    b = _tracker(db)
    b.reserve("ws-tenant", max_concurrency=1)
    b.release("ws-tenant")
    b.close()


def test_stale_slot_does_not_strand_forever(tmp_path: Path) -> None:
    """A slot reserved long ago by a dead worker stops counting after the bound."""
    db = tmp_path / "crawler.db"
    dead = CoordinationSyncSlotsStore(db)
    assert dead.reserve("ws-tenant", 1, 0, now=1.0) is not None
    dead.close()
    live = _tracker(db)
    live.reserve("ws-tenant", max_concurrency=1)
    live.release("ws-tenant")
    live.close()


def test_max_concurrency_zero_unbounded(tmp_path: Path) -> None:
    """max_concurrency==0 means unbounded: multiple reserves always succeed."""
    db = tmp_path / "crawler.db"
    a = _tracker(db)
    try:
        a.reserve("unbounded", max_concurrency=0)
        a.reserve("unbounded", max_concurrency=0)
        a.reserve("unbounded", max_concurrency=0)
        a.release("unbounded")
        a.release("unbounded")
        a.release("unbounded")
    finally:
        a.close()


def test_two_callers_never_block_each_other(tmp_path: Path) -> None:
    """Different callers must never block each other."""
    db = tmp_path / "crawler.db"
    a = _tracker(db)
    b = _tracker(db)
    try:
        a.reserve("caller-a", max_concurrency=1)
        b.reserve("caller-b", max_concurrency=1)
    finally:
        b.release("caller-b")
        b.close()
        a.release("caller-a")
        a.close()


def test_count_exactly_at_limit_allows(tmp_path: Path) -> None:
    """max_concurrency=2: exactly two reserves succeed, the third refuses."""
    db = tmp_path / "crawler.db"
    a = _tracker(db)
    try:
        a.reserve("multi", max_concurrency=2)
        a.reserve("multi", max_concurrency=2)
        with pytest.raises(QuotaRefusal):
            a.reserve("multi", max_concurrency=2)
    finally:
        a.release("multi")
        a.release("multi")
        a.close()
