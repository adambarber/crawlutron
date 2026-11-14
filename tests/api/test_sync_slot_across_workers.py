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


async def _reserve_and_refuse(db: Path, caller: str, max_conc: int) -> CallerConcurrencyTracker:
    """Reserve once then verify a second tracker is refused."""
    a = _tracker(db)
    await a.reserve(caller, max_concurrency=max_conc)
    with pytest.raises(QuotaRefusal):
        await _tracker(db).reserve(caller, max_concurrency=max_conc)
    return a


async def test_two_trackers_share_one_slot(tmp_path: Path) -> None:
    """Two tracker instances bound to the same DB: W=2 with max_concurrency=1
    must refuse the second admit."""
    db = tmp_path / "crawler.db"
    a = await _reserve_and_refuse(db, "ws-tenant", 1)
    await a.release("ws-tenant")


async def test_release_allows_spare_reserve(tmp_path: Path) -> None:
    """After releasing, the shared count falls and a spare can reserve."""
    db = tmp_path / "crawler.db"
    a = await _reserve_and_refuse(db, "ws-tenant", 1)
    await a.release("ws-tenant")
    b = _tracker(db)
    await b.reserve("ws-tenant", max_concurrency=1)
    await b.release("ws-tenant")
    b.close()


async def test_stale_slot_does_not_strand_forever(tmp_path: Path) -> None:
    """A slot reserved long ago by a dead worker stops counting after the bound."""
    db = tmp_path / "crawler.db"
    dead = CoordinationSyncSlotsStore(db)
    assert await dead.areserve("ws-tenant", 1, 0, now=1.0) is not None
    dead.close()
    live = _tracker(db)
    await live.reserve("ws-tenant", max_concurrency=1)
    await live.release("ws-tenant")
    live.close()


async def test_max_concurrency_zero_unbounded(tmp_path: Path) -> None:
    """max_concurrency==0 means unbounded: multiple reserves always succeed."""
    db = tmp_path / "crawler.db"
    a = _tracker(db)
    try:
        await a.reserve("unbounded", max_concurrency=0)
        await a.reserve("unbounded", max_concurrency=0)
        await a.reserve("unbounded", max_concurrency=0)
        await a.release("unbounded")
        await a.release("unbounded")
        await a.release("unbounded")
    finally:
        a.close()


async def test_two_callers_never_block_each_other(tmp_path: Path) -> None:
    """Different callers must never block each other."""
    db = tmp_path / "crawler.db"
    a = _tracker(db)
    b = _tracker(db)
    try:
        await a.reserve("caller-a", max_concurrency=1)
        await b.reserve("caller-b", max_concurrency=1)
    finally:
        await b.release("caller-b")
        b.close()
        await a.release("caller-a")
        a.close()


async def test_count_exactly_at_limit_allows(tmp_path: Path) -> None:
    """max_concurrency=2: exactly two reserves succeed, the third refuses."""
    db = tmp_path / "crawler.db"
    a = _tracker(db)
    try:
        await a.reserve("multi", max_concurrency=2)
        await a.reserve("multi", max_concurrency=2)
        with pytest.raises(QuotaRefusal):
            await a.reserve("multi", max_concurrency=2)
    finally:
        await a.release("multi")
        await a.release("multi")
        a.close()
