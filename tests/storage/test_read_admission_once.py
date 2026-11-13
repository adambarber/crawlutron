"""#519 item 3: a store read admits each distinct stored URL once, not per load."""

from __future__ import annotations

from safe_url.host_policy import HostPolicy

import crawlutron.storage.job_store_collections as collections
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.job_store_collections import FrontierEntry
from crawlutron.storage.job_store_state import CrawlJobState
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._policy_factories import job_store as store_on

_RESTRICTED = "example.com"
_OK_FRONTIER = ["https://example.com/", "https://example.com/ok"]


def _job_with_frontier(store: JobStore, urls: list[str]) -> CrawlJobState:
    """A saved job whose frontier holds ``urls`` at depth 1."""
    job = store.create("https://example.com/", {"limits": {"max_pages": 0}})
    job.frontier.extend(FrontierEntry(url=canonical(url), depth=1) for url in urls)
    store.save(job)
    return job


def _with_hostile(store: JobStore, job: CrawlJobState) -> CrawlJobState:
    """Append a private-address entry and save."""
    job.frontier.append(FrontierEntry(url=canonical("http://localhost:9/x"), depth=1))
    store.save(job)
    return job


def _hostile_job_on(db) -> CrawlJobState:
    """A saved job whose frontier holds one ok URL plus a private-address entry."""
    unrestricted = store_on(db, policy=UNRESTRICTED_HOST_POLICY)
    return _with_hostile(unrestricted, _job_with_frontier(unrestricted, ["https://example.com/ok"]))


def _restricted_load_frontier(db, job_id: str) -> list[str]:
    """Load under the example.com-restricted policy; return the frontier raws."""
    store = store_on(db, policy=HostPolicy.from_strings(_RESTRICTED, ""))
    reloaded = store.load(job_id)
    assert reloaded is not None
    return [entry.url.raw for entry in reloaded.frontier]


def test_two_loads_admit_each_distinct_url_once(monkeypatch, tmp_path):
    """Re-admitting every frontier entry per load is O(entries) per batch.

    After the fix, the second identical load performs no fresh frontier
    admission: the once-per-(raw, policy) admission cache answers the repeat."""
    store = store_on(tmp_path / "jobs.db")
    urls = [f"https://example.com/page{i}" for i in range(30)]
    job = _job_with_frontier(store, urls)

    calls: list[str] = []
    real = collections.admit_or_none

    def counting(candidate, *, policy):
        if isinstance(candidate, str):
            calls.append(candidate)
        return real(candidate, policy=policy)

    monkeypatch.setattr(collections, "admit_or_none", counting)

    first = store.load(job.job_id)
    first_count = len(calls)
    assert first is not None and len(first.frontier) == len(urls) + 1  # + root at depth 0
    assert first_count >= len(urls)

    calls.clear()
    second = store.load(job.job_id)
    assert second is not None and len(second.frontier) == len(urls) + 1
    re_admitted = [c for c in calls if c in urls]
    assert re_admitted == [], f"the second load re-admitted the frontier: {re_admitted[:5]}"


def test_a_hostile_stored_url_is_refused_on_every_read(tmp_path):
    """Trust on read never resurrects a URL the policy refuses: a row whose
    stored frontier holds a private address loads without the entry."""
    db = tmp_path / "jobs.db"
    job = _hostile_job_on(db)
    assert _restricted_load_frontier(db, job.job_id) == _OK_FRONTIER


def test_a_hostile_url_admitted_under_permissive_first_is_still_dropped(tmp_path):
    """#529: the cache is keyed per (raw, policy), not per raw. A raw first
    admitted under the unrestricted policy (the permissive-first read here)
    must still be refused when the restricted policy loads the same row."""
    db = tmp_path / "jobs.db"
    job = _hostile_job_on(db)

    # A permissive caller reads the row first: the cache now holds the raw as
    # admitted under UNRESTRICTED_HOST_POLICY.
    permissive = store_on(db, policy=UNRESTRICTED_HOST_POLICY)
    permissive_read = permissive.load(job.job_id)
    assert permissive_read is not None
    assert any(entry.url.raw == "http://localhost:9/x" for entry in permissive_read.frontier)

    assert _restricted_load_frontier(db, job.job_id) == _OK_FRONTIER
