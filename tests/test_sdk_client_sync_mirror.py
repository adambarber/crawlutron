"""Hermetic behavior pins for the sync SDK client's endpoint twins over
httpx.MockTransport (no server). The parity contract lock lives in
``test_sdk_client.py``; this module pins each twin's wire behavior.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import httpx

from crawlutron_sdk import CrawlutronClientSync
from crawlutron_sdk.models import (
    AgentHintsResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
    MapRequest,
    RenderRequest,
    SearchRequest,
    SearchResponse,
    SiteMapResponse,
    WatchCreateRequest,
    WatchListResponse,
    WatchState,
)


@contextmanager
def _served_sync_client(handler) -> Generator[CrawlutronClientSync, None, None]:
    """Yield a sync client over a MockTransport, closing it on exit."""
    client = CrawlutronClientSync(transport=httpx.MockTransport(handler), base_url="http://test")
    try:
        yield client
    finally:
        client.close()


def _assert_sent(request: httpx.Request, method: str, path: str) -> None:
    """Pin the wire method and path a handler saw."""
    assert request.method == method
    assert request.url.path == path


_CRAWL_BODY = {
    "request_id": "abc",
    "submitted_count": 1,
    "policy_schema_version": "1",
    "success": True,
    "results": [],
}


def _watch_state(watch_id: str = "w1") -> dict[str, object]:
    return {
        "watch_id": watch_id,
        "owner": "u",
        "url": "https://example.com/",
        "interval_seconds": 60,
        "status": "active",
        "next_check_at": 1.0,
        "last_checked_at": None,
        "created_at": 1.0,
        "updated_at": 1.0,
    }


def _job_status(job_id: str = "j1") -> dict[str, object]:
    return {
        "job_id": job_id,
        "root_url": "https://example.com/",
        "owner": "u",
        "status": "running",
        "kind": "resumable",
        "created_at": 1.0,
        "updated_at": 1.0,
        "frontier_size": 1,
        "visited_size": 1,
        "results_count": 1,
    }


def test_sync_create_job_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "POST", "/v1/jobs")
        body = request.read().decode()
        assert '"root_url":"https://example.com/"' in body
        return httpx.Response(
            200,
            json={"job_id": "j1", "status": "running", "root_url": "https://example.com/", "created_at": 1.0},
        )

    with _served_sync_client(handler) as client:
        response = client.create_job(JobCreateRequest(root_url="https://example.com/"))
    assert isinstance(response, JobCreateResponse)
    assert response.job_id == "j1"
    assert response.status == "running"


def test_sync_list_jobs_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "GET", "/v1/jobs")
        return httpx.Response(200, json={"jobs": [_job_status()], "total": 1})

    with _served_sync_client(handler) as client:
        response = client.list_jobs()
    assert isinstance(response, JobListResponse)
    assert response.total == 1
    assert response.jobs[0].job_id == "j1"


def test_sync_get_job_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "GET", "/v1/jobs/j1")
        return httpx.Response(200, json=_job_status("j1"))

    with _served_sync_client(handler) as client:
        response = client.get_job("j1")
    assert isinstance(response, JobStatusResponse)
    assert response.status == "running"


def test_sync_control_job_posts_action():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "POST", "/v1/jobs/j1/control")
        body = request.read().decode()
        assert '"action":"cancel"' in body
        return httpx.Response(200, json=_job_status("j1"))

    with _served_sync_client(handler) as client:
        response = client.control_job("j1", "cancel")
    assert isinstance(response, JobStatusResponse)
    assert response.status == "running"


def test_sync_delete_job_sends_delete():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "DELETE", "/v1/jobs/j1")
        return httpx.Response(200, json={"deleted": True, "job_id": "j1"})

    with _served_sync_client(handler) as client:
        client.delete_job("j1")


def test_sync_search_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "POST", "/v1/search")
        body = request.read().decode()
        assert '"query":"hello"' in body
        return httpx.Response(
            200,
            json={
                "success": True,
                "query": "hello",
                "provider": "x",
                "limit": 2,
                "data": [{"url": "https://example.com/", "title": "t", "description": "d"}],
            },
        )

    with _served_sync_client(handler) as client:
        response = client.search(SearchRequest(query="hello"))
    assert isinstance(response, SearchResponse)
    assert response.data[0].url == "https://example.com/"


def test_sync_create_watch_returns_state():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "POST", "/v1/watches")
        return httpx.Response(200, json=_watch_state("w1"))

    with _served_sync_client(handler) as client:
        response = client.create_watch(WatchCreateRequest(url="https://example.com/", interval_seconds=60))
    assert isinstance(response, WatchState)
    assert response.watch_id == "w1"
    assert response.status == "active"


def test_sync_list_watches_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "GET", "/v1/watches")
        return httpx.Response(200, json={"watches": [_watch_state()]})

    with _served_sync_client(handler) as client:
        response = client.list_watches()
    assert isinstance(response, WatchListResponse)
    assert response.watches[0].watch_id == "w1"


def test_sync_get_watch_returns_state():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "GET", "/v1/watches/w1")
        return httpx.Response(200, json=_watch_state("w1"))

    with _served_sync_client(handler) as client:
        response = client.get_watch("w1")
    assert isinstance(response, WatchState)
    assert response.watch_id == "w1"


def test_sync_delete_watch_sends_delete():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "DELETE", "/v1/watches/w1")
        return httpx.Response(200, json={"deleted": True, "watch_id": "w1"})

    with _served_sync_client(handler) as client:
        client.delete_watch("w1")


def test_sync_agent_hints_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "GET", "/v1/agent-hints")
        payload = {
            "service": "crawlutron",
            "strategies": {},
            "extractions": {},
            "guidance": [],
            "example_policies": {},
        }
        return httpx.Response(200, json=payload)

    with _served_sync_client(handler) as client:
        response = client.agent_hints()
    assert isinstance(response, AgentHintsResponse)
    assert response.service == "crawlutron"


def test_sync_map_snapshot_posts_frontier_request():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "POST", "/v1/map")
        assert '"url":"https://example.com/"' in request.read().decode()
        return httpx.Response(200, json={"url": "https://example.com/", "links": ["https://example.com/a"]})

    with _served_sync_client(handler) as client:
        response = client.map_snapshot(MapRequest(url="https://example.com/"))
    assert isinstance(response, SiteMapResponse)
    assert "https://example.com/a" in response.links


def test_sync_render_posts_url_and_parses_crawl_response():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "POST", "/v1/render")
        assert '"url":"https://example.com/"' in request.read().decode()
        return httpx.Response(200, json=_CRAWL_BODY)

    with _served_sync_client(handler) as client:
        response = client.render(RenderRequest(url="https://example.com/"))
    assert response.request_id == "abc"


def test_sync_policy_suggestions_gets_rollup():
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_sent(request, "GET", "/v1/policy-suggestions")
        return httpx.Response(200, json={"records_examined": 0, "suggestions": []})

    with _served_sync_client(handler) as client:
        response = client.policy_suggestions()
    assert response.records_examined == 0


def test_sync_list_jobs_passes_limit_and_offset_query():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jobs": [], "total": 0})

    with _served_sync_client(handler) as client:
        client.list_jobs(limit=10, offset=5)
    assert dict(seen[0].url.params) == {"limit": "10", "offset": "5"}
