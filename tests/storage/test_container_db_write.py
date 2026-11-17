"""EffectContainer.db_write routes SQLite writes off the event-loop thread.

Guardrail: sync ``txn_write`` raises ``WriteRejected`` from the loop. Async code routes writes
through ``EffectContainer.db_write`` (``asyncio.to_thread`` under the DB_WRITE semaphore, width 1).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.service.effects import EffectContainer
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.safe_db import WriteRejected, txn_write
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier


def make_job_store(tmp_path, container: EffectContainer | None = None) -> JobStore:
    """A JobStore wired to ``container`` (or a fresh one), ready for async writes."""
    return JobStore(
        tmp_path / "jobs.db",
        policy=UNRESTRICTED_HOST_POLICY,
        tier=deployment_tier(),
        container=container or EffectContainer(),
    )


async def test_txn_write_rejected_from_event_loop(tmp_path) -> None:
    """Calling txn_write directly from the loop raises WriteRejected (guardrail)."""
    import sqlite3

    store = make_job_store(tmp_path)
    try:
        conn = sqlite3.connect(str(store.db_path), isolation_level=None)
        try:
            with pytest.raises(WriteRejected, match="event-loop thread"):
                with txn_write(conn, owner="test"):
                    pass
        finally:
            conn.close()
    finally:
        store.close()


async def test_container_db_write_accepts_writes_from_async(tmp_path) -> None:
    """container.db_write runs a sync txn off the loop; async code never touches txn_write."""
    container = EffectContainer()
    store = make_job_store(tmp_path, container)
    try:
        job_id = "test-job-db-write"
        await container.db_write(
            lambda: store.create_single_shot(job_id, "http://example.com/", CrawlPolicy()),
            owner="test.create",
            timeout_seconds=5.0,
        )
        loaded = await store.aload(job_id)
        assert loaded is not None
        assert loaded.job_id == job_id
    finally:
        store.close()


@pytest.mark.real_sleeps
async def test_container_db_write_serializes_concurrent_writes(tmp_path) -> None:
    """Two concurrent db_write calls serialize under the width-1 writer semaphore."""
    container = EffectContainer()
    store = make_job_store(tmp_path, container)
    try:
        seen: list[str] = []
        lock = threading.Lock()

        def txn_one() -> None:
            with lock:
                seen.append("one-begin")
            time.sleep(0.02)
            with lock:
                seen.append("one-end")

        def txn_two() -> None:
            with lock:
                seen.append("two-begin")
            time.sleep(0.02)
            with lock:
                seen.append("two-end")

        await asyncio.gather(
            container.db_write(txn_one, owner="one", timeout_seconds=5.0),
            container.db_write(txn_two, owner="two", timeout_seconds=5.0),
        )
        assert seen == [
            "one-begin",
            "one-end",
            "two-begin",
            "two-end",
        ] or seen == [
            "two-begin",
            "two-end",
            "one-begin",
            "one-end",
        ]
    finally:
        store.close()


def test_container_db_write_claim_oblivious() -> None:
    """The container accepts any owner string; it never inspects claims."""
    container = EffectContainer()
    ran = []

    def txn() -> None:
        ran.append(True)

    async def run() -> None:
        await container.db_write(txn, owner="anything at all", timeout_seconds=1.0)

    asyncio.run(run())
    assert ran == [True]


def test_container_db_write_no_allow_on_loop_import() -> None:
    """allow_on_loop / allow_on_loop_read are escape hatches only, not in the container path."""
    import crawlutron.service.effects as effects
    import crawlutron.storage.job_store as job_store_mod

    assert not hasattr(effects, "allow_on_loop")
    assert not hasattr(effects, "allow_on_loop_read")
    assert "allow_on_loop" not in job_store_mod.__dict__
