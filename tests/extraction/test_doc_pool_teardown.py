"""Teardown closes the pool and reaps workers with the lock released."""

from __future__ import annotations

import threading

import pytest

import crawlutron.extraction.doc_pool as dp
from crawlutron.extraction.doc_pool import DocExtractionPool
from tests._process_doubles import StubSpawner, stub_worker


def test_teardown_terminates_workers_outside_the_lock(monkeypatch):
    """The retire loop must not hold the pool lock.

    ``terminate_doc_process`` can take seconds (grace, escalation), so holding
    the lock across it blocks every acquisition. Asserted behaviorally: while
    one teardown is parked mid-terminate, a concurrent ``acquire`` still
    completes -- it reads the closed pool's answer, not the lock."""
    worker = stub_worker(31337)
    spawner = StubSpawner(worker)
    pool = DocExtractionPool(spawn=spawner, worker_count=lambda: 1, enabled=lambda: True)
    acquired = pool.acquire(0.1)
    assert acquired is not None and acquired is worker
    pool.release(acquired)

    parked = threading.Event()
    release = threading.Event()

    def parked_terminate(proc, conn=None):
        parked.set()
        release.wait()  # rendezvous: no timeout, the test releases it
        proc.terminate()

    monkeypatch.setattr(dp, "terminate_doc_process", parked_terminate)

    teardown_done: list[bool] = []

    def _teardown() -> None:
        pool.teardown()
        teardown_done.append(True)

    teardown = threading.Thread(target=_teardown)
    teardown.start()
    assert parked.wait(10), "teardown never reached the worker termination"

    # A concurrent acquisition must not block behind the parked termination.
    probe_done: list[object] = []
    probe = threading.Thread(target=lambda: probe_done.append(pool.acquire(0.0)))
    probe.start()
    probe.join(timeout=5)
    assert probe_done, "acquire was blocked by the lock held across worker termination"
    assert probe_done == [None], "a closed pool must degrade to None, not resurrect"

    release.set()
    teardown.join(timeout=10)
    assert teardown_done, "teardown never completed"
    assert len(pool) == 0, "teardown forgot the slots"


@pytest.mark.parametrize("seed", [0, 2])
def test_teardown_forgets_slots_and_closes_the_pool(seed):
    """After teardown the pool holds no slots and every later acquire degrades."""
    spawner = StubSpawner(*[stub_worker(pid) for pid in range(seed)])
    pool = DocExtractionPool(spawn=spawner, worker_count=lambda: seed or 1, enabled=lambda: True)
    for _ in range(seed):
        slot = pool.acquire(0.1)
        assert slot is not None
        pool.release(slot)
    pool.teardown()

    assert len(pool) == 0
    assert pool.acquire(0.0) is None, "a closed pool must stay closed"
    pool.teardown()  # a second teardown is safe
    assert pool.acquire(0.0) is None
