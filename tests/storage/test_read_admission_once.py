"""#519 item 3: a store read admits each distinct stored URL once, not per load."""

from __future__ import annotations

import app.storage.job_store_collections as collections
from app.models.host_policy import HostPolicy
from app.storage.job_store import JobStore
from app.storage.job_store_collections import FrontierEntry
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._policy_factories import job_store as store_on


def _job_with_frontier(store: JobStore, urls: list[str]) -> JobStore:
    """A saved job whose frontier holds ``urls`` at depth 1."""
    job = store.create("https://example.com/", {"limits": {"max_pages": 0}})
    job.frontier.extend(FrontierEntry(url=canonical(url), depth=1) for url in urls)
    store.save(job)
    return job


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
    unrestricted = store_on(tmp_path / "jobs.db", policy=UNRESTRICTED_HOST_POLICY)
    job = _job_with_frontier(unrestricted, ["https://example.com/ok"])
    job.frontier.append(FrontierEntry(url=canonical("http://localhost:9/x"), depth=1))
    unrestricted.save(job)

    restricted = store_on(tmp_path / "jobs.db", policy=HostPolicy.from_strings("example.com", ""))
    reloaded = restricted.load(job.job_id)

    assert reloaded is not None
    assert [entry.url.raw for entry in reloaded.frontier] == ["https://example.com", "https://example.com/ok"]
