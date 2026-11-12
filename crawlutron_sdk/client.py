"""Typed async HTTP facade over a running crawlutron service: JSON in, envelopes out, typed errors. The
server is the sole enforcement chokepoint -- the SDK reimplements no admission, policy, robots, metering,
or extraction logic. One lazy httpx.AsyncClient; sync() mirrors it with a blocking httpx.Client.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Literal, Never, TypedDict, TypeVar

import httpx
from pydantic import BaseModel

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

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _ClientKwargs(TypedDict):
    """The shared httpx client constructor options."""

    base_url: str
    timeout: float | None
    headers: dict[str, str]


def _str_key_dict(data: object) -> dict[str, object]:
    """Coerce a decoded JSON value to a string-keyed dict."""
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items()}


def _json_body(response: httpx.Response) -> dict[str, object]:
    """Decode a JSON response body without leaking httpx's ``Any``."""
    if not response.content:
        return {}
    return _str_key_dict(response.json())


def _payload_dict(payload: object) -> dict[str, object]:
    """Dump a pydantic model to a plain dict for JSON serialization."""
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        return _str_key_dict(dump())
    return {}


def _job_list_query(limit: int | None, offset: int) -> str:
    """Build the GET /v1/jobs query suffix from the caller's page knobs."""
    parts: list[str] = []
    if limit is not None:
        parts.append(f"limit={limit}")
    if offset:
        parts.append(f"offset={offset}")
    return f"?{'&'.join(parts)}" if parts else ""


class _ClientBase:
    """Shared configuration, response handling, and error mapping."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float | None = None,
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
        self._raise_for_status(response.status_code, _json_body(response))

    def _finish(
        self,
        response: httpx.Response,
        model: type[_ModelT],
        *,
        accepted_if_pending: bool = False,
    ) -> _ModelT:
        """Validate one response body against ``model`` after status checking."""
        body = _json_body(response)
        self._raise_for_status(response.status_code, body, accepted_if_pending=accepted_if_pending)
        return model.model_validate(body)

    def _timeout_error(self, request_id: str, max_wait: float) -> CrawlutronTransportError:
        return CrawlutronTransportError(
            f"crawl {request_id} did not reach a terminal state within {max_wait}s",
            status_code=None,
        )

    def _transport_error(self, error: httpx.HTTPError) -> CrawlutronTransportError:
        return CrawlutronTransportError(str(error), status_code=None)

    def _transport_failure(self, error: httpx.HTTPError) -> Never:
        raise self._transport_error(error) from error

    def _check_max_wait(self, request_id: str, elapsed: float, max_wait: float | None) -> None:
        if max_wait is not None and elapsed >= max_wait:
            raise self._timeout_error(request_id, max_wait)


class CrawlutronClient(_ClientBase):
    """Async typed client: ``async with CrawlutronClient() as client: ...``."""

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
        try:
            return await client.request(method, path, json=json)
        except httpx.HTTPError as error:
            raise self._transport_error(error) from error

    async def crawl(self, payload: CrawlRequest) -> CrawlResponse:
        """Synchronous crawl door: returns the unified crawl response."""
        response = await self._request("POST", "/v1/crawl", json=_payload_dict(payload))
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    async def crawl_async(self, payload: CrawlRequest) -> CrawlAccepted:
        """Async crawl door: returns the accepted handle (request_id, status_url)."""
        request = payload.model_copy(update={"async_mode": True})
        response = await self._request("POST", "/v1/crawl", json=_payload_dict(request))
        return self._finish(response, CrawlAccepted)

    async def get_crawl_result(self, request_id: str) -> CrawlResponse:
        """Retrieve a completed crawl; raises if still accepted (202) or gone (404)."""
        response = await self._request("GET", f"/v1/crawl/{request_id}")
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    async def wait_for(
        self, request_id: str, *, poll_interval: float = 1.0, max_wait: float | None = None
    ) -> CrawlResponse:
        """Poll ``get_crawl_result`` until the crawl reaches a terminal state."""
        elapsed = 0.0
        while True:
            try:
                return await self.get_crawl_result(request_id)
            except CrawlutronAcceptedError:
                pass
            self._check_max_wait(request_id, elapsed, max_wait)
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    async def crawl_stream(self, payload: CrawlRequest) -> AsyncIterator[CrawlResponse]:
        """Crawl asynchronously and yield the unified response when complete."""
        accepted = await self.crawl_async(payload)
        yield await self.wait_for(accepted.request_id)

    async def admit(self, payload: AdmitRequest) -> AdmitResponse:
        """Pre-flight admission verdict for one URL."""
        response = await self._request("POST", "/v1/admit", json=_payload_dict(payload))
        return self._finish(response, AdmitResponse)

    async def map_snapshot(self, payload: MapRequest) -> SiteMapResponse:
        """Discover the URL frontier for a root URL (POST /v1/map)."""
        response = await self._request("POST", "/v1/map", json=_payload_dict(payload))
        return self._finish(response, SiteMapResponse)

    async def render(self, payload: RenderRequest) -> CrawlResponse:
        """Browser-render one or more pages (POST /v1/render)."""
        response = await self._request("POST", "/v1/render", json=_payload_dict(payload))
        return self._finish(response, CrawlResponse)

    async def policy_suggestions(self) -> PolicySuggestionsResponse:
        """Propose candidate host policy rules (GET /v1/policy-suggestions)."""
        response = await self._request("GET", "/v1/policy-suggestions")
        return self._finish(response, PolicySuggestionsResponse)

    async def create_job(self, payload: JobCreateRequest) -> JobCreateResponse:
        """Create and start a resumable whole-site crawl job."""
        response = await self._request("POST", "/v1/jobs", json=_payload_dict(payload))
        return self._finish(response, JobCreateResponse)

    async def list_jobs(self, limit: int | None = None, offset: int = 0) -> JobListResponse:
        """List the caller's crawl jobs; ``limit``/``offset`` page the result."""
        response = await self._request("GET", f"/v1/jobs{_job_list_query(limit, offset)}")
        return self._finish(response, JobListResponse)

    async def get_job(self, job_id: str) -> JobStatusResponse:
        """Get one crawl job's state and results."""
        response = await self._request("GET", f"/v1/jobs/{job_id}")
        return self._finish(response, JobStatusResponse)

    async def control_job(
        self, job_id: str, action: Literal["pause", "resume", "cancel"]
    ) -> JobStatusResponse:
        """Pause/resume/cancel a crawl job."""
        payload = JobControlRequest(action=action)
        response = await self._request("POST", f"/v1/jobs/{job_id}/control", json=_payload_dict(payload))
        return self._finish(response, JobStatusResponse)

    async def delete_job(self, job_id: str) -> None:
        """Cancel (if running) and delete a crawl job."""
        response = await self._request("DELETE", f"/v1/jobs/{job_id}")
        self._check(response)

    async def search(self, payload: SearchRequest) -> SearchResponse:
        """Run a web search against the configured provider."""
        response = await self._request("POST", "/v1/search", json=_payload_dict(payload))
        return self._finish(response, SearchResponse)

    async def create_watch(self, payload: WatchCreateRequest) -> WatchState:
        """Register a URL revalidation watch."""
        response = await self._request("POST", "/v1/watches", json=_payload_dict(payload))
        return self._finish(response, WatchState)

    async def list_watches(self) -> WatchListResponse:
        """List the caller's registered watches."""
        response = await self._request("GET", "/v1/watches")
        return self._finish(response, WatchListResponse)

    async def get_watch(self, watch_id: str) -> WatchState:
        """Get one watch's revalidation state."""
        response = await self._request("GET", f"/v1/watches/{watch_id}")
        return self._finish(response, WatchState)

    async def delete_watch(self, watch_id: str) -> None:
        """Stop checking a watch's URL."""
        response = await self._request("DELETE", f"/v1/watches/{watch_id}")
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
    """Blocking typed mirror of :class:`CrawlutronClient`: sync twins for every
    public endpoint; async-only surfaces (``crawl_stream``, ``aclose``) are absent.
    """

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
        try:
            return client.request(method, path, json=json)
        except httpx.HTTPError as error:
            self._transport_failure(error)

    def crawl(self, payload: CrawlRequest) -> CrawlResponse:
        response = self._request("POST", "/v1/crawl", json=_payload_dict(payload))
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    def crawl_async(self, payload: CrawlRequest) -> CrawlAccepted:
        request = payload.model_copy(update={"async_mode": True})
        response = self._request("POST", "/v1/crawl", json=_payload_dict(request))
        return self._finish(response, CrawlAccepted)

    def get_crawl_result(self, request_id: str) -> CrawlResponse:
        """Retrieve a completed crawl; raises if still accepted (202) or gone (404)."""
        response = self._request("GET", f"/v1/crawl/{request_id}")
        return self._finish(response, CrawlResponse, accepted_if_pending=True)

    def wait_for(
        self, request_id: str, *, max_wait: float | None = None, poll_interval: float = 1.0
    ) -> CrawlResponse:
        elapsed = 0.0
        while True:
            try:
                return self.get_crawl_result(request_id)
            except CrawlutronAcceptedError:
                pass
            self._check_max_wait(request_id, elapsed, max_wait)
            time.sleep(poll_interval)
            elapsed += poll_interval

    def admit(self, payload: AdmitRequest) -> AdmitResponse:
        response = self._request("POST", "/v1/admit", json=_payload_dict(payload))
        return self._finish(response, AdmitResponse)

    def map_snapshot(self, payload: MapRequest) -> SiteMapResponse:
        response = self._request("POST", "/v1/map", json=_payload_dict(payload))
        return self._finish(response, SiteMapResponse)

    def render(self, payload: RenderRequest) -> CrawlResponse:
        response = self._request("POST", "/v1/render", json=_payload_dict(payload))
        return self._finish(response, CrawlResponse)

    def policy_suggestions(self) -> PolicySuggestionsResponse:
        response = self._request("GET", "/v1/policy-suggestions")
        return self._finish(response, PolicySuggestionsResponse)

    def create_job(self, payload: JobCreateRequest) -> JobCreateResponse:
        response = self._request("POST", "/v1/jobs", json=_payload_dict(payload))
        return self._finish(response, JobCreateResponse)

    def list_jobs(self, limit: int | None = None, offset: int = 0) -> JobListResponse:
        response = self._request("GET", f"/v1/jobs{_job_list_query(limit, offset)}")
        return self._finish(response, JobListResponse)

    def get_job(self, job_id: str) -> JobStatusResponse:
        response = self._request("GET", f"/v1/jobs/{job_id}")
        return self._finish(response, JobStatusResponse)

    def control_job(self, job_id: str, action: Literal["pause", "resume", "cancel"]) -> JobStatusResponse:
        payload = JobControlRequest(action=action)
        response = self._request("POST", f"/v1/jobs/{job_id}/control", json=_payload_dict(payload))
        return self._finish(response, JobStatusResponse)

    def delete_job(self, job_id: str) -> None:
        response = self._request("DELETE", f"/v1/jobs/{job_id}")
        self._check(response)

    def search(self, payload: SearchRequest) -> SearchResponse:
        response = self._request("POST", "/v1/search", json=_payload_dict(payload))
        return self._finish(response, SearchResponse)

    def create_watch(self, payload: WatchCreateRequest) -> WatchState:
        response = self._request("POST", "/v1/watches", json=_payload_dict(payload))
        return self._finish(response, WatchState)

    def list_watches(self) -> WatchListResponse:
        response = self._request("GET", "/v1/watches")
        return self._finish(response, WatchListResponse)

    def get_watch(self, watch_id: str) -> WatchState:
        response = self._request("GET", f"/v1/watches/{watch_id}")
        return self._finish(response, WatchState)

    def delete_watch(self, watch_id: str) -> None:
        response = self._request("DELETE", f"/v1/watches/{watch_id}")
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
