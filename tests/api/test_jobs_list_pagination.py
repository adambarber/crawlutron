"""GET /v1/jobs pagination: a bounded page, exact slices, and the caller total.

The list route returns a caller's jobs newest-first (created_at DESC). Pagination
must not re-materialize the whole per-caller set per request; the page size is capped.
"""

from __future__ import annotations

import pytest

from app.vocabulary.limits import DEFAULT_JOB_LIST_LIMIT, MAX_JOB_LIST_LIMIT
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _seed_jobs(job_store, count: int, owner: str = "") -> list[str]:
    """Create ``count`` jobs for ``owner`` (default the anonymous caller) and
    return their job_ids, oldest first. The bare `client` fixture is anonymous."""
    out: list[str] = []
    for i in range(count):
        job = job_store.create(f"https://example.test/job-{i}", UNRESTRICTED_HOST_POLICY, owner=owner)
        out.append(job.job_id)
    return out


async def test_page_2_returns_the_exact_slice(client, job_store):
    job_ids = _seed_jobs(job_store, 12)

    resp = await client.get("/v1/jobs", params={"limit": 5, "offset": 5})
    assert resp.status_code == 200
    body = resp.json()
    # Newest-first: the seed order is oldest->newest, so offset 5 skips the 5 newest.
    assert body["total"] == 12
    assert [row["job_id"] for row in body["jobs"]] == list(reversed(job_ids))[5:10]


async def test_offset_past_the_end_returns_an_empty_page(client, job_store):
    _seed_jobs(job_store, 3)

    resp = await client.get("/v1/jobs", params={"limit": 5, "offset": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["jobs"] == []


async def test_a_keyed_caller_never_sees_another_callers_jobs(client, job_store):
    """A keyed caller never sees another caller's jobs, paged or not."""
    job_store.create("https://example.test/foreign", UNRESTRICTED_HOST_POLICY, owner="other")
    job_ids = _seed_jobs(job_store, 2)

    resp = await client.get("/v1/jobs", params={"limit": 10, "offset": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [row["job_id"] for row in body["jobs"]] == list(reversed(job_ids))


@pytest.mark.parametrize("limit", [MAX_JOB_LIST_LIMIT + 1, 0], ids=["above-the-cap", "below-one"])
async def test_out_of_range_limits_are_rejected(client, job_store, limit):
    resp = await client.get("/v1/jobs", params={"limit": limit})
    assert resp.status_code == 422


async def test_default_limit_caps_the_page(client, job_store):
    _seed_jobs(job_store, DEFAULT_JOB_LIST_LIMIT + 5)

    resp = await client.get("/v1/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == DEFAULT_JOB_LIST_LIMIT
    assert body["total"] == DEFAULT_JOB_LIST_LIMIT + 5
