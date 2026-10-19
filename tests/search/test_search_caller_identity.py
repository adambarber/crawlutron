"""#298: the search door is caller-scoped -- identity, concurrency, and metrics.
Before this change /v1/search reached the provider with no caller identity: a
wrong key got 200 and one caller could fan out unlimited provider calls."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.api.caller_concurrency import CallerConcurrencyTracker
from app.api.dependencies import get_job_store, get_settings
from app.config.settings import CrawlerSettings
from app.storage.caller_key_store import CallerKeyStore
from tests._async_harnesses import expect_quota_refused, issue_key, serve
from tests._policy_factories import job_store as make_job_store


@contextlib.asynccontextmanager
async def _search_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    """An app serving /v1/search + /v1/keys over isolated stores, Tavily configured."""
    from app.api.identity_router import router as identity_router
    from app.api.keys_router import router as keys_router

    keys = CallerKeyStore(tmp_path / "keys.db")
    jobs = make_job_store(tmp_path / "jobs.db")
    app = FastAPI()
    app.include_router(identity_router)
    app.include_router(keys_router)
    app.state.caller_key_store = keys
    app.state.key_admin_token = "test-admin-token"
    # ONE per-caller register, named like the service's: the search door counts
    # against the same in-flight ceiling as the caller's sync crawls.
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    app.dependency_overrides[get_job_store] = lambda: jobs
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping(
        {"CRAWLUTRON_SEARCH_PROVIDER": "tavily", "CRAWLUTRON_TAVILY_API_KEY": "test-key"}
    )
    async with serve(app) as client:
        yield client, app


def _admin() -> dict[str, str]:
    return {"X-API-Key": "test-admin-token"}


async def _issue(client: AsyncClient, *, max_concurrency: int = 4) -> str:
    """Issue a key with ``max_concurrency`` and return its secret; the funnel."""
    return await issue_key(client, max_concurrency=max_concurrency)


def _expect_quota_refused(response) -> None:
    """The 429 quota shape, read through the funnel."""
    return expect_quota_refused(response)


def _provider_body() -> str:
    return json.dumps({"results": [{"title": "A", "url": "https://example.gov/a", "content": "desc"}]})


def _stub_search_provider(monkeypatch, handler) -> None:
    """Point the search provider HTTP at ``handler`` so no real network is hit."""
    import app.search.providers as providers_mod

    monkeypatch.setattr(providers_mod, "provider_http", handler)


async def _happy_provider(*args, **kwargs):
    return 200, _provider_body()


async def _search_response(tmp_path: Path, *, key: str | None = None) -> Response:
    """POST a minimal search over the isolated client and return the response."""
    headers = {"X-API-Key": key} if key else None
    async with _search_client(tmp_path) as (client, app):
        return await client.post("/v1/search", headers=headers, json={"query": "x"})


async def test_search_rejects_a_wrong_key(tmp_path, monkeypatch):
    """A caller with a key that does not exist gets 401, not a 200 search."""
    _stub_search_provider(monkeypatch, _happy_provider)
    response = await _search_response(tmp_path, key="no-such-key")
    assert response.status_code == 401, response.text


async def test_search_accepts_a_valid_key(tmp_path, monkeypatch):
    """A keyed caller's search succeeds and is resolved as authenticated."""
    _stub_search_provider(monkeypatch, _happy_provider)
    async with _search_client(tmp_path) as (client, app):
        secret = await _issue(client)
        response = await _search_response(tmp_path, key=secret)
        assert response.status_code == 200, response.text


async def test_search_anonymous_caller_is_bounded_and_succeeds(tmp_path, monkeypatch):
    """No header resolves to the anonymous bucket: the search still works."""
    _stub_search_provider(monkeypatch, _happy_provider)
    response = await _search_response(tmp_path)
    assert response.status_code == 200, response.text


async def test_search_concurrency_is_bounded_per_caller(tmp_path, monkeypatch):
    """Two concurrent searches from a caller with ``max_concurrency=1``: the second 429."""
    from tests._concurrency_probe import assert_second_concurrent_refused, make_blocking_callable

    blocker, entered, released = make_blocking_callable(result=(200, _provider_body()))
    _stub_search_provider(monkeypatch, blocker)
    async with _search_client(tmp_path) as (client, app):
        secret = await _issue(client, max_concurrency=1)
        await assert_second_concurrent_refused(
            client,
            url="/v1/search",
            headers={"X-API-Key": secret},
            body={"query": "x"},
            entered=entered,
            released=released,
            first_ok=(200,),
        )


async def test_search_is_refused_at_the_admission_cap(tmp_path, monkeypatch):
    """#340/#328: search routes through request_gate and refuses with a 503 at the cap.

    Search shares the cap's counter; it must take a slot and refuse at the bound
    like every trackable door, not count-and-proceed with a discarded bool.
    """
    from app.api import main_helpers
    from app.health.readiness import ReadinessState

    state = ReadinessState(max_active_requests=1)
    assert state.begin_request()  # fill the cap
    monkeypatch.setattr(main_helpers, "readiness_state", state)

    _stub_search_provider(monkeypatch, _happy_provider)
    response = await _search_response(tmp_path)
    assert response.status_code == 503, response.text
    assert "capacity_exceeded" in response.text
    # The refused request never held a slot: the cap population is unchanged.
    assert state.snapshot().active_requests == 1


async def test_search_readiness_slot_is_exactly_one_and_balanced(tmp_path, monkeypatch):
    """#328: a search holds exactly one readiness slot for its life, released after.

    The pairing must be exactly one begin/end per request regardless of door --
    a search that both gates and hand-counts would double-count the population.
    """
    from app.health.readiness import READINESS_STATE

    outside = READINESS_STATE.snapshot().active_requests
    held: list[int] = []

    async def _record_active(*args, **kwargs):
        held.append(READINESS_STATE.snapshot().active_requests)
        return 200, _provider_body()

    _stub_search_provider(monkeypatch, _record_active)
    response = await _search_response(tmp_path)
    assert response.status_code == 200, response.text

    assert held == [outside + 1]
    assert READINESS_STATE.snapshot().active_requests == outside
