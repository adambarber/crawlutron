"""Async-door quota across workers: max_concurrency binds over shared SQLite.

A second worker is refused while the first admission is held open; no race.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.service.caller_auth import Tracking, admit
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.errors import QuotaRefusal
from crawlutron.storage.job_store import JobStore
from tests._policy_factories import job_store as make_store

_CALLER = CallerIdentity(caller_id="test-caller", authenticated=True, max_concurrency=1)


class _Workers(NamedTuple):
    trackers: tuple[CallerConcurrencyTracker, CallerConcurrencyTracker]
    stores: tuple[JobStore, JobStore]


def _workers(tmp_path: Path) -> _Workers:
    """Two independent workers over one coordination DB and one job DB."""
    coord = tmp_path / "coord.db"
    db = tmp_path / "crawler.db"
    tracker_a = CallerConcurrencyTracker(coordination_db_path=coord)
    tracker_b = CallerConcurrencyTracker(coordination_db_path=coord)
    return _Workers((tracker_a, tracker_b), (make_store(db), make_store(db)))


def _doorways(workers: _Workers) -> tuple[Tracking, Tracking]:
    """Tracking pairs for the two workers, in worker order."""
    (tracker_a, tracker_b), (store_a, store_b) = workers
    return Tracking(job_store=store_a, concurrency_tracker=tracker_a), Tracking(
        job_store=store_b, concurrency_tracker=tracker_b
    )


@pytest.fixture
def workers(tmp_path: Path) -> Iterator[_Workers]:
    """Two workers over shared DBs, closed after the test even on failure."""
    pair = _workers(tmp_path)
    try:
        yield pair
    finally:
        for tracker in pair.trackers:
            tracker.close()
        for store in pair.stores:
            store.close()


async def test_cross_worker_quota_holds_until_release(workers: _Workers) -> None:
    """A held admission blocks another worker; release restores its capacity."""
    first, second = _doorways(workers)
    async with admit(_CALLER, 1, tracking=first):
        with pytest.raises(QuotaRefusal, match="quota"):
            async with admit(_CALLER, 1, tracking=second):
                pass
    async with admit(_CALLER, 1, tracking=second) as caller_id:
        assert caller_id == _CALLER.caller_id
