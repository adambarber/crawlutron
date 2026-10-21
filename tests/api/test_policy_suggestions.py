"""Suggestions come from what the crawls actually recorded; each test owns its
result store (the rollup reads every record), and nothing here applies one
-- the service proposes, a caller owns host policy.
"""

import logging
from typing import Any

import pytest

from app.api.policy_suggestions import policy_suggestions
from app.config.settings import CrawlerSettings
from app.storage.result_store import CrawlResultStore
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


async def _suggestion_for(client, base_url) -> dict[str, Any]:
    """The endpoint's suggestion for this test's host."""
    body = (await client.get("/v1/policy-suggestions")).json()
    return next(s for s in body["suggestions"] if s["host"] == canonical(base_url).site)


async def test_a_repeatedly_blocked_host_is_suggested_with_its_evidence(client, base_url):
    await _crawl_twice(client, f"{base_url}/blocked")

    response = await client.get("/v1/policy-suggestions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["records_examined"] == 2
    suggestion = await _suggestion_for(client, base_url)
    assert suggestion["rule"] == "denied_hosts"
    assert suggestion["blocked_attempts"] == 2
    assert suggestion["observed_attempts"] == 2
    # The evidence a reviewer is being asked to trust is the crawler's own: the classification that fired and
    # the signature that matched it.
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

    suggestion = await _suggestion_for(client, base_url)
    assert suggestion["blocked_attempts"] == 1


async def test_a_blocked_attempt_among_good_ones_is_counted_against_the_host(client, base_url):
    await completed_crawl(client, make_crawl_payload(f"{base_url}/blocked"))
    await completed_crawl(client, make_crawl_payload(f"{base_url}/blog"))

    suggestion = await _suggestion_for(client, base_url)
    assert suggestion["blocked_attempts"] == 1
    assert suggestion["observed_attempts"] == 2


async def test_no_retained_records_means_no_suggestions(client):
    """The endpoint answers on an empty store instead of 404ing: a service that
    has crawled nothing has nothing to suggest, which is a fact about the
    evidence, not a missing resource."""
    response = await client.get("/v1/policy-suggestions")

    assert response.status_code == 200, response.text
    assert response.json() == {"records_examined": 0, "records_skipped": 0, "suggestions": []}


async def test_one_malformed_retained_row_does_not_kill_the_rollup(tmp_path, caplog):
    """Per-record isolation (#212): a row that fails to validate is skipped and
    reported rather than 500ing every suggestion from the same store.

    The window's size is reported honestly, and the skip names the row.
    """
    store = CrawlResultStore(tmp_path / "results.db")
    good_page = {
        "normalized_url": "https://good.test/x",
        "status": "blocked",
        "classification": "access_denied",
        "selected_strategy": None,
        "attempted_strategies": [],
        "classification_evidence": [],
    }
    await store.aput("crawl_good", {"request_id": "crawl_good", "results": [good_page]})
    # A retained row whose results are not a list -- TraceResponse.model_validate rejects it.
    await store.aput("crawl_bad", {"request_id": "crawl_bad", "results": "not-a-list"})

    body = await store.apayloads()
    assert len(body) == 2
    with caplog.at_level(logging.WARNING):
        response = await policy_suggestions(result_store=store, settings=CrawlerSettings.from_mapping({}))
    assert response.records_examined == 2
    assert response.records_skipped == 1
    assert len(response.suggestions) == 1
    assert response.suggestions[0].host == canonical("https://good.test/x").site
    skipped = [r for r in caplog.records if r.getMessage() == "policy_suggestions_skip_malformed"]
    assert [r.request_id for r in skipped] == ["crawl_bad"], "the skip did not name the row it skipped"


async def test_a_window_of_only_malformed_rows_is_not_an_empty_window(tmp_path):
    """Every retained row unreadable is not the same answer as nothing retained.

    Both answer 200 with no suggestions, so the counts are what tell them apart.
    """
    store = CrawlResultStore(tmp_path / "all-bad.db")
    await store.aput("crawl_bad", {"request_id": "crawl_bad", "results": "not-a-list"})

    response = await policy_suggestions(result_store=store, settings=CrawlerSettings.from_mapping({}))

    assert response.records_examined == 1
    assert response.records_skipped == 1
    assert response.suggestions == []


async def test_an_expired_record_is_not_offered_as_evidence(tmp_path):
    """A record the store has already declared gone must not keep proposing a
    rule: the TTL that retires a result retires its evidence with it."""
    now = [1_000.0]
    store = CrawlResultStore(tmp_path / "expired-results.db", ttl_seconds=10, clock=lambda: now[0])
    await store.aput("crawl_old", {"request_id": "crawl_old", "results": []})

    assert await store.apayloads() != []
    now[0] += 11

    assert await store.apayloads() == []
