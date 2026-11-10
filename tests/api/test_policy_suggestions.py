"""Suggestions come from what the crawls actually recorded; each test owns its
result store (the rollup reads every record), and nothing here applies one
-- the service proposes, a caller owns host policy.
"""

import contextlib
import logging
from typing import cast

import pytest

from crawlutron.api.policy_suggestions import policy_suggestions
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.json_value import JsonValue
from crawlutron.storage.result_store import CrawlResultStore
from tests._assertions import completed_crawl
from tests._fetcher_factories import make_crawl_payload
from tests._policy_factories import canonical
from tests._policy_harnesses import policy_client
from tests._policy_harnesses import stored_trace as _stored_trace
from tests._quota_harness import TEST_ADMIN_TOKEN, keyed_result_client


@pytest.fixture
def result_store(tmp_path) -> CrawlResultStore:
    return CrawlResultStore(tmp_path / "results.db")


#: The default keyed caller the endpoint tests crawl and roll up as; records the
#: crawl endpoint stores ride the same identity, so scoping keeps them visible.
KEY_A = CallerIdentity(caller_id="key_a", authenticated=True)


@pytest.fixture
async def client(result_store, job_store):
    async with policy_client(result_store, job_store, caller=KEY_A) as crawl_client:
        yield crawl_client


async def _crawl_twice(client, url: str) -> None:
    for _ in range(2):
        await completed_crawl(client, make_crawl_payload(url))


async def _suggestion_for(client, base_url) -> dict[str, object]:
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
    classifications = suggestion["classifications"]
    assert isinstance(classifications, list)
    assert "access_denied" in classifications
    assert suggestion["signatures"]
    evidence = suggestion["evidence"]
    assert isinstance(evidence, list) and evidence
    first_evidence = evidence[0]
    assert isinstance(first_evidence, dict) and first_evidence["context"]
    rationale = suggestion["rationale"]
    assert isinstance(rationale, str)
    host = suggestion["host"]
    assert isinstance(host, str)
    assert host in rationale


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
    good_page: dict[str, JsonValue] = {
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

    body = await store.apayloads_page(limit=10)
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

    assert await store.apayloads_page(limit=10) != []
    now[0] += 11

    assert await store.apayloads_page(limit=10) == []


class _PagingStore(CrawlResultStore):
    """The real store, recording the bounded page reads the rollup makes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_calls: list[int] = []

    def payloads_page(self, *, limit: int, cursor=None, owner=None):
        self.page_calls.append(limit)
        return super().payloads_page(limit=limit, cursor=cursor, owner=owner)


async def test_the_rollup_reads_bounded_pages_never_the_whole_table(tmp_path):
    """#443: one GET must not stream the table; the rollup scans bounded pages."""
    store = _PagingStore(tmp_path / "paged.db")
    store.put("crawl_blocked", _stored_trace("crawl_blocked", "https://page.test/x"))

    response = await policy_suggestions(result_store=store, settings=CrawlerSettings.from_mapping({}))

    assert store.page_calls, "the rollup read the store without paging"
    # Page size stays bounded (_PAGE_SIZE); the window cap is the other bound.
    assert max(store.page_calls) <= 50, store.page_calls
    assert response.records_examined == 1
    assert [s.host for s in response.suggestions] == [canonical("https://page.test/x").site]


async def test_rows_past_the_read_window_are_reported_not_hidden(tmp_path):
    """#449: the rollup reads a bounded window, newest first; the rows it does
    not reach are reported as skipped, never silently read or silently hidden."""
    now = [1000.0]
    store = CrawlResultStore(tmp_path / "window.db", ttl_seconds=3600, clock=lambda: now[0])
    # The oldest row is the ONLY evidence for old.test: one past the window.
    now[0] += 1
    store.put("crawl_old", _stored_trace("crawl_old", "https://old.test/"))
    # The newest 500 rows are the window (_MAX_RECORDS): all new.test evidence.
    for i in range(500):
        now[0] += 1
        store.put(f"crawl_{i}", _stored_trace(f"crawl_{i}", f"https://new.test/{i}"))

    response = await policy_suggestions(result_store=store, settings=CrawlerSettings.from_mapping({}))

    assert response.records_examined == 501
    assert response.records_skipped == 1, "the unread row was hidden or read anyway"
    assert [s.host for s in response.suggestions] == [canonical("https://new.test/").site]


async def test_validation_and_rollup_run_off_the_event_loop(tmp_path, monkeypatch):
    """#449: per-row pydantic validation and the host rollup are CPU work;
    either running on the loop stalls every concurrent request."""
    import asyncio

    from crawlutron.models.normalized_url import admit_or_none
    from crawlutron.models.trace_response import TraceResponse

    store = CrawlResultStore(tmp_path / "offloop.db")
    store.put("crawl_blocked", _stored_trace("crawl_blocked", "https://offloop.test/"))

    on_loop: list[str] = []
    real_validate = TraceResponse.model_validate
    real_admit = admit_or_none

    def _validate_spy(value):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real_validate(value)
        on_loop.append("model_validate")
        return real_validate(value)

    def _admit_spy(candidate, *, policy):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real_admit(candidate, policy=policy)
        on_loop.append("admit_or_none")
        return real_admit(candidate, policy=policy)

    monkeypatch.setattr(TraceResponse, "model_validate", _validate_spy)
    monkeypatch.setattr("crawlutron.api.policy_suggestions.admit_or_none", _admit_spy)

    await policy_suggestions(result_store=store, settings=CrawlerSettings.from_mapping({}))

    assert on_loop == [], f"loop-thread work: {on_loop}"


async def test_an_unauthenticated_request_is_refused(result_store, job_store):
    """#514: no key, no rollup -- the evidence surface is not anonymous."""
    async with policy_client(result_store, job_store) as client:
        response = await client.get("/v1/policy-suggestions")

    assert response.status_code == 401, response.text


_FOREIGN_TRACE_NAME = "crawl_foreign"


def _assert_cross_tenant(response) -> None:
    """The operator verdict: 200, the foreign record examined, its host suggested."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["records_examined"] == 1
    assert [s["host"] for s in body["suggestions"]] == [canonical("https://foreign.test/blocked").site]


def _foreign_row() -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", _stored_trace(_FOREIGN_TRACE_NAME, "https://foreign.test/blocked"))


async def test_a_keyed_caller_does_not_see_another_callers_evidence(result_store, job_store):
    """#514: a keyed non-operator caller's rollup covers only its own records."""
    await result_store.aput(_FOREIGN_TRACE_NAME, _foreign_row(), owner="key_b")

    async with policy_client(result_store, job_store, caller=KEY_A) as client:
        body = (await client.get("/v1/policy-suggestions")).json()

    assert body["suggestions"] == [], "a foreign caller's evidence leaked into the rollup"
    assert body["records_examined"] == 0


async def _operator_rollup(result_store, job_store, *, bearer: bool):
    """One operator rollup over the foreign record; the credential law's two
    presentations are the parameter -- the verdict is the same either way."""
    await result_store.aput(_FOREIGN_TRACE_NAME, _foreign_row(), owner="key_b")
    headers = {"Authorization": "Bearer test-admin-token"} if bearer else {"X-API-Key": "test-admin-token"}
    async with policy_client(
        result_store,
        job_store,
        caller=KEY_A,
        key_admin_token="test-admin-token",
    ) as client:
        return await client.get("/v1/policy-suggestions", headers=headers)


async def test_the_operator_token_sees_the_cross_tenant_rollup(result_store, job_store):
    """#514: the full rollup is operator-tier, mirroring the /metrics detail gate."""
    response = await _operator_rollup(result_store, job_store, bearer=False)
    _assert_cross_tenant(response)


async def test_the_bearer_presented_operator_token_sees_the_cross_tenant_rollup(result_store, job_store):
    """#676: the credential law is X-API-Key OR Bearer; the split-brain operator
    parse only saw x-api-key, so a Bearer admin token silently got the scoped
    per-tenant view instead of the cross-tenant one."""
    response = await _operator_rollup(result_store, job_store, bearer=True)
    _assert_cross_tenant(response)


@contextlib.asynccontextmanager
async def _real_door_client(tmp_path):
    """The REAL /v1/policy-suggestions door: a live key store and settings, no
    identity override -- every credential class resolves through get_caller_identity."""
    from crawlutron.api.keys_router import router as keys_router
    from crawlutron.api.policy_router import router as policy_router

    async with keyed_result_client(tmp_path, (policy_router, keys_router)) as (client, results, keys):
        yield client, keys, results


async def test_the_operator_token_reaches_the_cross_tenant_rollup_through_the_real_door(tmp_path):
    """The operator token on the real CallerDep door: 200 with the cross-tenant
    rollup. Before the operator arm existed in the identity resolver, this very
    request 401'd (invalid_api_key) -- the documented operator view was
    unreachable by anyone."""
    async with _real_door_client(tmp_path) as (client, keys, results):
        await results.aput(_FOREIGN_TRACE_NAME, _foreign_row(), owner="key_b")

        response = await client.get("/v1/policy-suggestions", headers={"X-API-Key": TEST_ADMIN_TOKEN})

    _assert_cross_tenant(response)


async def test_a_live_caller_key_still_gets_the_per_tenant_rollup(tmp_path):
    """Boundary class 2: a live caller key through the real door is 200 with its
    own records only -- the operator arm must not disturb the keyed path."""
    async with _real_door_client(tmp_path) as (client, keys, results):
        issued = keys.issue_key("key_a")
        await results.aput(_FOREIGN_TRACE_NAME, _foreign_row(), owner="key_b")

        response = await client.get("/v1/policy-suggestions", headers={"X-API-Key": issued.secret})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggestions"] == [], "a foreign caller's evidence leaked into the rollup"
    assert body["records_examined"] == 0


async def test_no_credential_is_401_through_the_real_door(tmp_path):
    """Boundary class 1: no credential is refused exactly as before."""
    async with _real_door_client(tmp_path) as (client, _keys, _results):
        response = await client.get("/v1/policy-suggestions")

    _assert_401_envelope(response)


async def test_a_wrong_credential_is_401_through_the_real_door(tmp_path):
    """Boundary class 4: a presented credential that is neither the operator
    token nor a stored caller key is a 401 -- wrong scheme (Basic), garbage
    key, and the operator token with no token configured all refuse."""
    async with _real_door_client(tmp_path) as (client, _keys, _results):
        basic = await client.get("/v1/policy-suggestions", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        garbage = await client.get("/v1/policy-suggestions", headers={"X-API-Key": "not-any-key"})
        # The bearer form of a garbage credential refuses too.
        bearer = await client.get("/v1/policy-suggestions", headers={"Authorization": "Bearer not-any-key"})

    for response in (basic, garbage, bearer):
        _assert_401_envelope(response)


def _assert_401_envelope(response) -> None:
    """The 401 refusal shape every unauthenticated caller reads."""
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "invalid_api_key"
