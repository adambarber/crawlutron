"""Typed async HTTP facade over a running crawlutron service."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal, Never, TypedDict, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from ._client_support import job_list_query, json_body, payload_dict, retry_wait
from .errors import CrawlutronAcceptedError, CrawlutronTransportError, error_from_response
from .models import (
    AdmitRequest,
    AdmitResponse,
    AgentHintsResponse,
    CapabilitiesResponse,
    CrawlAccepted,
    CrawlRequest,
    CrawlResponse,
    HealthResponse,
    JobControlRequest,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
    MapRequest,
    PolicySuggestionsResponse,
    ReadinessResponse,
    RenderRequest,
    SearchRequest,
    SearchResponse,
    SiteMapResponse,
    WatchCreateRequest,
    WatchListResponse,
    WatchState,
)

_DEFAULT_BASE_URL = "http://127.0.0.1:8765"
_DEFAULT_TIMEOUT = 300.0


async def _poll_result(
    request_id: str,
    fetch: Callable[[], Awaitable[CrawlResponse]],
    deadline: float | None,
    poll_interval: float,
    timeout_factory: Callable[[str], CrawlutronTransportError],
) -> CrawlResponse:
    while True:
        try:
            return await fetch()
        except CrawlutronAcceptedError:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            raise timeout_factory(request_id)
        await asyncio.sleep(poll_interval)


_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _ClientKwargs(TypedDict):
    base_url: str
    timeout: float | None
    headers: dict[str, str]


class _ClientBase:
    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float | None = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.Client | httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["X-API-Key"] = self._api_key
        return headers

    def _client_kwargs(self) -> _ClientKwargs:
        return {
            "base_url": self._base_url,
            "timeout": self._timeout,
            "headers": self._headers(),
        }

    def _raise_for_status(
        self,
        status_code: int,
        body: dict[str, object],
        *,
        accepted_if_pending: bool = False,
    ) -> None:
        """Raise typed errors; with ``accepted_if_pending`` also refuse a 202."""
        if accepted_if_pending and status_code == 202:
            raise error_from_response(status_code, body)
        if status_code < 400:
            return
        raise error_from_response(status_code, body)

    def _check(self, response: httpx.Response) -> None:
        """Raise typed errors for a response with no body to return."""
        self._raise_for_status(response.status_code, json_body(response))

    def _finish(
        self,
        response: httpx.Response,
        model: type[_ModelT],
        *,
        accepted_if_pending: bool = False,
    ) -> _ModelT:
        """Validate one response body against ``model`` after status checking."""
        body = json_body(response)
        self._raise_for_status(response.status_code, body, accepted_if_pending=accepted_if_pending)
        return model.model_validate(body)

    def _transport_error(self, error: httpx.HTTPError) -> CrawlutronTransportError:
        return CrawlutronTransportError(str(error), status_code=None)

    def _transport_failure(self, error: httpx.HTTPError) -> Never:
        raise self._transport_error(error) from error

    def _make_timeout(self, request_id: str, max_wait: float | None = None) -> CrawlutronTransportError:
        bound = f" within {max_wait}s" if max_wait is not None else ""
        return CrawlutronTransportError(
            f"crawl {request_id} did not reach a terminal state{bound}", status_code=None
        )

    def _make_wait_deadline(self, max_wait: float | None) -> float | None:
        if max_wait is not None:
            return time.monotonic() + max_wait
        return None


class CrawlutronClient(_ClientBase):
    _client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> CrawlutronClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None or not isinstance(client, httpx.AsyncClient):
            transport = self._transport
            if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
                raise CrawlutronTransportError(
                    "sync-only transport cannot drive an async client",
                    status_code=None,
                )
            self._client = httpx.AsyncClient(**self._client_kwargs(), transport=transport)
            client = self._client
        if not isinstance(client, httpx.AsyncClient):
            raise CrawlutronTransportError("unexpected http client type", status_code=None)
        return client

    async def aclose(self) -> None:
        client = self._client
        if client is None:
            return
        if not isinstance(client, httpx.AsyncClient):
            raise CrawlutronTransportError("unexpected http client type", status_code=None)
        await client.aclose()
        self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        client = await self._get_client()
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                response = await client.request(method, path, json=json)
            except httpx.HTTPError as error:
                raise self._transport_error(error) from error
            if response.status_code in (429, 503):
                if attempt < max_retries:
                    await asyncio.sleep(retry_wait(response))
                    continue
                break
            break
        return response

    async def crawl(self, payload: CrawlRequest) -> CrawlResponse:
        """Synchronous crawl door: returns the unified crawl response."""
        response = await self._request("POST", "/v1/crawl", json=payload_dict(payload))
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    async def crawl_async(self, payload: CrawlRequest) -> CrawlAccepted:
        """Async crawl door: returns the accepted handle (request_id, status_url)."""
        request = payload.model_copy(update={"async_mode": True})
        response = await self._request("POST", "/v1/crawl", json=payload_dict(request))
        return self._finish(response, CrawlAccepted)

    async def get_crawl_result(self, request_id: str) -> CrawlResponse:
        """Retrieve a completed crawl; raises if still accepted (202) or gone (404)."""
        response = await self._request("GET", f"/v1/crawl/{quote(request_id, safe='')}")
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    async def wait_for(
        self, request_id: str, *, poll_interval: float = 1.0, max_wait: float | None = 60.0
    ) -> CrawlResponse:
        """Poll ``get_crawl_result`` until the crawl reaches a terminal state."""
        return await _poll_result(
            request_id,
            lambda: self.get_crawl_result(request_id),
            self._make_wait_deadline(max_wait),
            poll_interval,
            lambda rid: self._make_timeout(rid, max_wait),
        )

    async def crawl_and_wait(self, payload: CrawlRequest) -> CrawlResponse:
        """Crawl asynchronously and wait for the unified response."""
        accepted = await self.crawl_async(payload)
        return await self.wait_for(accepted.request_id)

    async def admit(self, payload: AdmitRequest) -> AdmitResponse:
        """Pre-flight admission verdict for one URL."""
        response = await self._request("POST", "/v1/admit", json=payload_dict(payload))
        return self._finish(response, AdmitResponse)

    async def map_snapshot(self, payload: MapRequest) -> SiteMapResponse:
        """Discover the URL frontier for a root URL (POST /v1/map)."""
        response = await self._request("POST", "/v1/map", json=payload_dict(payload))
        return self._finish(response, SiteMapResponse)

    async def render(self, payload: RenderRequest) -> CrawlResponse:
        """Browser-render one or more pages (POST /v1/render)."""
        response = await self._request("POST", "/v1/render", json=payload_dict(payload))
        return self._finish(response, CrawlResponse)

    async def policy_suggestions(self) -> PolicySuggestionsResponse:
        """Propose candidate host policy rules (GET /v1/policy-suggestions)."""
        response = await self._request("GET", "/v1/policy-suggestions")
        return self._finish(response, PolicySuggestionsResponse)

    async def create_job(self, payload: JobCreateRequest) -> JobCreateResponse:
        """Create and start a resumable whole-site crawl job."""
        response = await self._request("POST", "/v1/jobs", json=payload_dict(payload))
        return self._finish(response, JobCreateResponse)

    async def list_jobs(self, limit: int | None = None, offset: int = 0) -> JobListResponse:
        """List the caller's crawl jobs; ``limit``/``offset`` page the result."""
        response = await self._request("GET", f"/v1/jobs{job_list_query(limit, offset)}")
        return self._finish(response, JobListResponse)

    async def get_job(self, job_id: str) -> JobStatusResponse:
        """Get one crawl job's state and results."""
        response = await self._request("GET", f"/v1/jobs/{quote(job_id, safe='')}")
        return self._finish(response, JobStatusResponse)

    async def control_job(
        self, job_id: str, action: Literal["pause", "resume", "cancel"]
    ) -> JobStatusResponse:
        """Pause/resume/cancel a crawl job."""
        payload = JobControlRequest(action=action)
        response = await self._request(
            "POST", f"/v1/jobs/{quote(job_id, safe='')}/control", json=payload_dict(payload)
        )
        return self._finish(response, JobStatusResponse)

    async def delete_job(self, job_id: str) -> None:
        """Cancel (if running) and delete a crawl job."""
        response = await self._request("DELETE", f"/v1/jobs/{quote(job_id, safe='')}")
        self._check(response)

    async def search(self, payload: SearchRequest) -> SearchResponse:
        """Run a web search against the configured provider."""
        response = await self._request("POST", "/v1/search", json=payload_dict(payload))
        return self._finish(response, SearchResponse)

    async def create_watch(self, payload: WatchCreateRequest) -> WatchState:
        """Register a URL revalidation watch."""
        response = await self._request("POST", "/v1/watches", json=payload_dict(payload))
        return self._finish(response, WatchState)

    async def list_watches(self) -> WatchListResponse:
        """List the caller's registered watches."""
        response = await self._request("GET", "/v1/watches")
        return self._finish(response, WatchListResponse)

    async def get_watch(self, watch_id: str) -> WatchState:
        """Get one watch's revalidation state."""
        response = await self._request("GET", f"/v1/watches/{quote(watch_id, safe='')}")
        return self._finish(response, WatchState)

    async def delete_watch(self, watch_id: str) -> None:
        """Stop checking a watch's URL."""
        response = await self._request("DELETE", f"/v1/watches/{quote(watch_id, safe='')}")
        self._check(response)

    async def capabilities(self) -> CapabilitiesResponse:
        """Service capabilities discovery (GET /v1/capabilities)."""
        response = await self._request("GET", "/v1/capabilities")
        return self._finish(response, CapabilitiesResponse)

    async def agent_hints(self) -> AgentHintsResponse:
        """Agent guidance (GET /v1/agent-hints)."""
        response = await self._request("GET", "/v1/agent-hints")
        return self._finish(response, AgentHintsResponse)

    async def health(self) -> HealthResponse:
        """Lightweight liveness (GET /health)."""
        response = await self._request("GET", "/health")
        return self._finish(response, HealthResponse)

    async def ready(self) -> ReadinessResponse:
        """Crawl capacity/readiness (GET /ready)."""
        response = await self._request("GET", "/ready")
        return self._finish(response, ReadinessResponse)

    def sync(self) -> CrawlutronClientSync:
        """A sync client sharing this client's configuration."""
        return CrawlutronClientSync(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
            transport=self._transport,
        )


class CrawlutronClientSync(_ClientBase):
    _client: httpx.Client | None = None

    def __enter__(self) -> CrawlutronClientSync:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get_client(self) -> httpx.Client:
        client = self._client
        if not isinstance(client, httpx.Client):
            transport = self._transport
            if transport is not None and not isinstance(transport, httpx.BaseTransport):
                raise CrawlutronTransportError(
                    "async-only transport cannot drive a sync client; pass an httpx.BaseTransport",
                    status_code=None,
                )
            self._client = httpx.Client(**self._client_kwargs(), transport=transport)
            client = self._client
        return client

    def close(self) -> None:
        if self._client is None:
            return
        client = self._client
        if not isinstance(client, httpx.Client):
            raise CrawlutronTransportError("unexpected http client type", status_code=None)
        client.close()
        self._client = None

    def _request(self, method: str, path: str, *, json: dict[str, object] | None = None) -> httpx.Response:
        client = self._get_client()
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                response = client.request(method, path, json=json)
            except httpx.HTTPError as error:
                self._transport_failure(error)
            if response.status_code in (429, 503):
                if attempt < max_retries:
                    time.sleep(retry_wait(response))
                    continue
                break
            break
        return response

    def crawl(self, payload: CrawlRequest) -> CrawlResponse:
        response = self._request("POST", "/v1/crawl", json=payload_dict(payload))
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    def crawl_async(self, payload: CrawlRequest) -> CrawlAccepted:
        request = payload.model_copy(update={"async_mode": True})
        response = self._request("POST", "/v1/crawl", json=payload_dict(request))
        return self._finish(response, CrawlAccepted)

    def get_crawl_result(self, request_id: str) -> CrawlResponse:
        """Retrieve a completed crawl; raises if still accepted (202) or gone (404)."""
        response = self._request("GET", f"/v1/crawl/{quote(request_id, safe='')}")
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    def wait_for(
        self, request_id: str, *, max_wait: float | None = 60.0, poll_interval: float = 1.0
    ) -> CrawlResponse:
        """Poll ``get_crawl_result`` until the crawl reaches a terminal state."""
        deadline = self._make_wait_deadline(max_wait)
        while True:
            try:
                return self.get_crawl_result(request_id)
            except CrawlutronAcceptedError:
                pass
            if deadline is not None and time.monotonic() >= deadline:
                raise self._make_timeout(request_id, max_wait)
            time.sleep(poll_interval)

    def admit(self, payload: AdmitRequest) -> AdmitResponse:
        response = self._request("POST", "/v1/admit", json=payload_dict(payload))
        return self._finish(response, AdmitResponse)

    def map_snapshot(self, payload: MapRequest) -> SiteMapResponse:
        response = self._request("POST", "/v1/map", json=payload_dict(payload))
        return self._finish(response, SiteMapResponse)

    def render(self, payload: RenderRequest) -> CrawlResponse:
        response = self._request("POST", "/v1/render", json=payload_dict(payload))
        return self._finish(response, CrawlResponse)

    def policy_suggestions(self) -> PolicySuggestionsResponse:
        response = self._request("GET", "/v1/policy-suggestions")
        return self._finish(response, PolicySuggestionsResponse)

    def create_job(self, payload: JobCreateRequest) -> JobCreateResponse:
        response = self._request("POST", "/v1/jobs", json=payload_dict(payload))
        return self._finish(response, JobCreateResponse)

    def list_jobs(self, limit: int | None = None, offset: int = 0) -> JobListResponse:
        response = self._request("GET", f"/v1/jobs{job_list_query(limit, offset)}")
        return self._finish(response, JobListResponse)

    def get_job(self, job_id: str) -> JobStatusResponse:
        response = self._request("GET", f"/v1/jobs/{quote(job_id, safe='')}")
        return self._finish(response, JobStatusResponse)

    def control_job(self, job_id: str, action: Literal["pause", "resume", "cancel"]) -> JobStatusResponse:
        payload = JobControlRequest(action=action)
        response = self._request(
            "POST", f"/v1/jobs/{quote(job_id, safe='')}/control", json=payload_dict(payload)
        )
        return self._finish(response, JobStatusResponse)

    def delete_job(self, job_id: str) -> None:
        response = self._request("DELETE", f"/v1/jobs/{quote(job_id, safe='')}")
        self._check(response)

    def search(self, payload: SearchRequest) -> SearchResponse:
        response = self._request("POST", "/v1/search", json=payload_dict(payload))
        return self._finish(response, SearchResponse)

    def create_watch(self, payload: WatchCreateRequest) -> WatchState:
        response = self._request("POST", "/v1/watches", json=payload_dict(payload))
        return self._finish(response, WatchState)

    def list_watches(self) -> WatchListResponse:
        response = self._request("GET", "/v1/watches")
        return self._finish(response, WatchListResponse)

    def get_watch(self, watch_id: str) -> WatchState:
        response = self._request("GET", f"/v1/watches/{quote(watch_id, safe='')}")
        return self._finish(response, WatchState)

    def delete_watch(self, watch_id: str) -> None:
        response = self._request("DELETE", f"/v1/watches/{quote(watch_id, safe='')}")
        self._check(response)

    def agent_hints(self) -> AgentHintsResponse:
        response = self._request("GET", "/v1/agent-hints")
        return self._finish(response, AgentHintsResponse)

    def capabilities(self) -> CapabilitiesResponse:
        response = self._request("GET", "/v1/capabilities")
        return self._finish(response, CapabilitiesResponse)

    def health(self) -> HealthResponse:
        response = self._request("GET", "/health")
        return self._finish(response, HealthResponse)

    def ready(self) -> ReadinessResponse:
        response = self._request("GET", "/ready")
        return self._finish(response, ReadinessResponse)


__all__ = [
    "CrawlutronClient",
    "CrawlutronClientSync",
]
