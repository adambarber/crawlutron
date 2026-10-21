"""Every store family locks on its database's identity, not the path it spells.

One parameterization over the families: the property and its proof are the
same, so two copies of a proof are two proofs to keep true.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from app.models.crawl_policy import CrawlPolicy
from app.storage.coordination_counters import CoordinationCounters
from app.storage.job_store import JobStore
from app.storage.sqlite_store import process_lock, store_lock_key
from tests._policy_factories import job_store

#: The real store classes, bound by name so the proof cannot drift onto a shape
#: the stores do not have.
_Store = JobStore | CoordinationCounters
_Family = Callable[[], tuple[_Store, Callable[[], object]]]


def _job_family() -> tuple[JobStore, Callable[[], object]]:
    store = job_store(":memory:")
    return store, lambda: store.create("http://example.com/", CrawlPolicy())


def _counter_family() -> tuple[CoordinationCounters, Callable[[], object]]:
    store = CoordinationCounters(":memory:")
    return store, lambda: store.add("requests", 1.0)


_FAMILIES = pytest.mark.parametrize(
    "family",
    [_job_family, _counter_family],
    ids=["job-store", "coordination-store"],
)


@_FAMILIES
def test_two_inmemory_stores_take_distinct_locks(family: _Family) -> None:
    """Two unrelated in-memory stores must not serialize on one lock (#255):
    each store has its own private shared-cache database, so each locks on its
    own identity rather than the one path they all spell alike.
    """
    store_a, _ = family()
    store_b, _ = family()
    assert store_a.lock_key != store_b.lock_key


@_FAMILIES
def test_inmemory_store_locks_on_its_identity_not_the_shared_path(family: _Family) -> None:
    """The lock key names the store's own memory identity (#255): the path key
    every in-memory store spells alike would have put them all on one lock.
    """
    store, _ = family()
    assert store.lock_key.startswith("memory:")
    assert store.lock_key != store_lock_key(store.db_path)


@_FAMILIES
def test_a_store_write_waits_for_the_lock_it_names(family: _Family) -> None:
    """The store writes under the lock it names (#255): holding that lock holds
    the write, so the store and the migration that took the same key exclude
    each other.
    """
    store, write = family()
    done = threading.Event()

    def run() -> None:
        write()
        done.set()

    with process_lock(store.lock_key):
        worker = threading.Thread(target=run)
        worker.start()
        assert not done.wait(0.2)
    worker.join(timeout=5)
    assert done.is_set()
