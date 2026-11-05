"""Store construction and seeding: one door for JobStore, results, and rows."""

from __future__ import annotations

from crawlutron.models.json_value import JsonValue
from crawlutron.storage.job_store import JobStatus, JobStore
from tests._job_store_doubles import (
    InstrumentedJobStore,
    frontier,
    loaded_job,
    make_crawled_job_store,
    make_job_store,
    single_shot_job,
    single_shot_orphan,
)
from tests._policy_factories import job_store


def seed_owned_job(store: JobStore, job_id: str = "crawl_owned", owner: str = "key_a") -> None:
    """One single-shot job owned by ``owner``; every scoped test seeds the same row."""
    store.create_single_shot(job_id, "http://example.com/", {}, owner=owner)


def complete_job(store: JobStore, job_id: str, payload: JsonValue, owner: str = "") -> None:
    """Seed one owned single-shot job and drive it to COMPLETED with ``payload``."""
    seed_owned_job(store, job_id=job_id, owner=owner)
    done = store.load(job_id)
    assert done is not None
    done.results = [payload if isinstance(payload, dict) else {"payload": payload}]
    store.save(done)
    store.set_status(job_id, JobStatus.COMPLETED, expected=JobStatus.PENDING)


__all__ = [
    "InstrumentedJobStore",
    "complete_job",
    "frontier",
    "job_store",
    "loaded_job",
    "make_crawled_job_store",
    "make_job_store",
    "seed_owned_job",
    "single_shot_job",
    "single_shot_orphan",
]
