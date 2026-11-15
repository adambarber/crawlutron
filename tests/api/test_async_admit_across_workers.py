"""Async-door quota across workers: max_concurrency binds over shared SQLite.

Two admits at max_concurrency=1: BEGIN IMMEDIATE refuses the second.
"""

from __future__ import annotations

import asyncio

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.service.caller_auth import Tracking, admit
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.errors import QuotaRefusal
from tests._policy_factories import job_store as make_store

_CALLER = CallerIdentity(caller_id="test-caller", authenticated=True, max_concurrency=1)


async def _admit_once(tracker: CallerConcurrencyTracker, store) -> None:
    """One async-door admit; raises QuotaRefusal when the quota is full."""
    tracking = Tracking(job_store=store, concurrency_tracker=tracker)
    async with admit(_CALLER, 1, tracking=tracking):
        # The async door inserts its job row inside the admission; the slot
        # stands for that row until it lands.
        await asyncio.sleep(0)


async def test_async_admit_refuses_second_at_max_concurrency_one(tmp_path):
    """Two concurrent async admits at max_concurrency=1: one succeeds, one refused.

    The durable reservation under BEGIN IMMEDIATE is the cross-worker mutex.
    Bounded by asyncio.wait_for -- never an unbounded wait.
    """
    db = tmp_path / "crawler.db"
    coord = tmp_path / "coord.db"

    tracker_a = CallerConcurrencyTracker(coordination_db_path=coord)
    tracker_b = CallerConcurrencyTracker(coordination_db_path=coord)
    store_a = make_store(db)
    store_b = make_store(db)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _admit_once(tracker_a, store_a),
                _admit_once(tracker_b, store_b),
                return_exceptions=True,
            ),
            timeout=10.0,
        )
    finally:
        tracker_a.close()
        tracker_b.close()
        store_a.close()
        store_b.close()

    refusals = [r for r in results if isinstance(r, QuotaRefusal)]
    successes = [r for r in results if not isinstance(r, BaseException)]
    assert len(successes) == 1, f"expected exactly one successful admit, got {len(successes)}"
    assert len(refusals) == 1, f"expected exactly one QuotaRefusal, got {len(refusals)}"
    assert "quota" in str(refusals[0]).lower(), refusals[0]
