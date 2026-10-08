"""``GET /v1/policy-suggestions``: candidate host rules proposed from evidence.

The evidence is whatever the crawls actually recorded -- these tests crawl a
real blocked page through the real crawl door, so a suggestion here was proposed
from classification evidence the engine produced, not from a fixture describing
what evidence looks like. Each test owns its result store (see
``tests/_policy_harnesses``): the rollup reads every retained record, so a
shared store would let another test's crawl move the counts.

A suggestion is a candidate for a human to accept, edit or reject. Nothing in
this surface applies one: the service proposes rules, a caller owns host policy.
"""

import pytest

from app.crawl.result_store import CrawlResultStore
from tests._assertions import completed_crawl
from tests._fetcher_factories import make_crawl_payload
from tests._policy_factories import canonical
from tests._policy_harnesses import policy_client


@pytest.fixture
def result_store(tmp_path) -> CrawlResultStore:
    return CrawlResultStore(tmp_path / "results.db")


@pytest.fixture
async def client(result_store, job_store):
    async with policy_client(result_store, job_store) as crawl_client:
        yield crawl_client


async def _crawl_twice(client, url: str) -> None:
    for _ in range(2):
        await completed_crawl(client, make_crawl_payload(url))


async def test_a_repeatedly_blocked_host_is_suggested_with_its_evidence(client, base_url):
    await _crawl_twice(client, f"{base_url}/blocked")

    response = await client.get("/v1/policy-suggestions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["records_examined"] == 2
    suggestion = next(s for s in body["suggestions"] if s["host"] == canonical(base_url).site)
    assert suggestion["rule"] == "denied_hosts"
    assert suggestion["blocked_attempts"] == 2
    assert suggestion["observed_attempts"] == 2
    # The evidence a reviewer is being asked to trust is the crawler's own:
    # the classification that fired and the signature that matched it.
    assert "access_denied" in suggestion["classifications"]
    assert suggestion["signatures"]
    assert suggestion["evidence"][0]["context"]
    assert suggestion["host"] in suggestion["rationale"]


async def test_a_host_that_was_never_blocked_is_not_suggested(client, base_url):
    await completed_crawl(client, make_crawl_payload(f"{base_url}/blog"))

    body = (await client.get("/v1/policy-suggestions")).json()

    assert body["suggestions"] == []
    assert body["records_examined"] == 1


async def test_one_blocked_attempt_is_reported_with_an_honest_count(client, base_url):
    """No invented threshold: the accumulation is the count, and a reviewer
    decides whether one observation is worth a rule."""
    await completed_crawl(client, make_crawl_payload(f"{base_url}/blocked"))

    body = (await client.get("/v1/policy-suggestions")).json()

    suggestion = next(s for s in body["suggestions"] if s["host"] == canonical(base_url).site)
    assert suggestion["blocked_attempts"] == 1


async def test_a_blocked_attempt_among_good_ones_is_counted_against_the_host(client, base_url):
    await completed_crawl(client, make_crawl_payload(f"{base_url}/blocked"))
    await completed_crawl(client, make_crawl_payload(f"{base_url}/blog"))

    body = (await client.get("/v1/policy-suggestions")).json()

    suggestion = next(s for s in body["suggestions"] if s["host"] == canonical(base_url).site)
    assert suggestion["blocked_attempts"] == 1
    assert suggestion["observed_attempts"] == 2


async def test_no_retained_records_means_no_suggestions(client):
    """The endpoint answers on an empty store instead of 404ing: a service that
    has crawled nothing has nothing to suggest, which is a fact about the
    evidence, not a missing resource."""
    response = await client.get("/v1/policy-suggestions")

    assert response.status_code == 200, response.text
    assert response.json() == {"records_examined": 0, "suggestions": []}


async def test_an_expired_record_is_not_offered_as_evidence(tmp_path):
    """A record the store has already declared gone must not keep proposing a
    rule: the TTL that retires a result retires its evidence with it."""
    now = [1_000.0]
    store = CrawlResultStore(tmp_path / "expired-results.db", ttl_seconds=10, clock=lambda: now[0])
    await store.aput("crawl_old", {"request_id": "crawl_old", "results": []})

    assert await store.apayloads() != []
    now[0] += 11

    assert await store.apayloads() == []
