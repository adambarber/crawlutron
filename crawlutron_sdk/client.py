"""Typed async HTTP facade over a running crawlutron service.

The client serializes JSON, deserializes envelopes, and raises typed errors.
The server is the sole enforcement chokepoint; the SDK reimplements no
admission, policy, robots, metering, or extraction logic.

The client owns one :class:`httpx.AsyncClient` (lazily created, closed on
:meth:`aclose` / ``__aexit__``). :meth:`sync` returns a blocking mirror that
owns its own :class:`httpx.Client` configured identically.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Literal

import httpx

from .errors import (
    CrawlutronAcceptedError,
    CrawlutronTransportError,
    error_from_response,
)
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
    JobDeletedResponse,
    JobListResponse,
    JobStatusResponse,
    ReadinessResponse,
    SearchRequest,
    SearchResponse,
    WatchCreateRequest,
    WatchListResponse,
    WatchState,
)

_DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def _json_body(response: httpx.Response) -> dict[str, object]:
    """Decode a JSON response body without leaking httpx's ``Any``."""
    if not response.content:
        return {}
    data: object = response.json()
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return {}


def _payload_dict(payload: object) -> dict[str, object]:
    """Dump a pydantic model to a plain dict for JSON serialization."""
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items()}
    return {}


class _ClientBase:
    """Shared configuration and error handling for the async and sync clients."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["X-API-Key"] = self._api_key
        return headers

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

    def _transport_error(self, error: httpx.HTTPError) -> CrawlutronTransportError:
        return CrawlutronTransportError(str(error), status_code=None)


class CrawlutronClient(_ClientBase):
    """Async typed client: ``async with CrawlutronClient() as client: ...``."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> CrawlutronClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._headers(),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
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
        body = _json_body(response)
        self._raise_for_status(response.status_code, body, accepted_if_pending=True)
        return CrawlResponse.model_validate(body)

    async def crawl_async(self, payload: CrawlRequest) -> CrawlAccepted:
        """Async crawl door: returns the accepted handle (request_id, status_url)."""
        request = payload.model_copy(update={"async_mode": True})
        response = await self._request("POST", "/v1/crawl", json=_payload_dict(request))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return CrawlAccepted.model_validate(body)

    async def get_crawl_result(self, request_id: str) -> CrawlResponse:
        """Retrieve a completed crawl; raises if still accepted (202) or gone (404)."""
        response = await self._request("GET", f"/v1/crawl/{request_id}")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body, accepted_if_pending=True)
        return CrawlResponse.model_validate(body)

    async def wait_for(
        self,
        request_id: str,
        *,
        poll_interval: float = 1.0,
        max_wait: float | None = None,
    ) -> CrawlResponse:
        """Poll ``get_crawl_result`` until the crawl reaches a terminal state."""
        elapsed = 0.0
        while True:
            try:
                return await self.get_crawl_result(request_id)
            except CrawlutronAcceptedError:
                pass
            if max_wait is not None and elapsed >= max_wait:
                raise CrawlutronTransportError(
                    f"crawl {request_id} did not reach a terminal state within {max_wait}s",
                    status_code=None,
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    async def crawl_stream(self, payload: CrawlRequest) -> AsyncIterator[CrawlResponse]:
        """Crawl asynchronously and yield the unified response when complete."""
        accepted = await self.crawl_async(payload)
        yield await self.wait_for(accepted.request_id)

    async def admit(self, payload: AdmitRequest) -> AdmitResponse:
        """Pre-flight admission verdict for one URL."""
        response = await self._request("POST", "/v1/admit", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return AdmitResponse.model_validate(body)

    async def create_job(self, payload: JobCreateRequest) -> JobCreateResponse:
        """Create and start a resumable whole-site crawl job."""
        response = await self._request("POST", "/v1/jobs", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return JobCreateResponse.model_validate(body)

    async def list_jobs(self) -> JobListResponse:
        """List the caller's crawl jobs with live status."""
        response = await self._request("GET", "/v1/jobs")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return JobListResponse.model_validate(body)

    async def get_job(self, job_id: str) -> JobStatusResponse:
        """Get one crawl job's state and results."""
        response = await self._request("GET", f"/v1/jobs/{job_id}")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return JobStatusResponse.model_validate(body)

    async def control_job(
        self, job_id: str, action: Literal["pause", "resume", "cancel"]
    ) -> JobStatusResponse:
        """Pause/resume/cancel a crawl job."""
        payload = JobControlRequest(action=action)
        response = await self._request("POST", f"/v1/jobs/{job_id}/control", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return JobStatusResponse.model_validate(body)

    async def delete_job(self, job_id: str) -> JobDeletedResponse:
        """Cancel (if running) and delete a crawl job."""
        response = await self._request("DELETE", f"/v1/jobs/{job_id}")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return JobDeletedResponse.model_validate(body)

    async def search(self, payload: SearchRequest) -> SearchResponse:
        """Run a web search against the configured provider."""
        response = await self._request("POST", "/v1/search", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return SearchResponse.model_validate(body)

    async def create_watch(self, payload: WatchCreateRequest) -> WatchState:
        """Register a URL revalidation watch."""
        response = await self._request("POST", "/v1/watches", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return WatchState.model_validate(body)

    async def list_watches(self) -> WatchListResponse:
        """List the caller's registered watches."""
        response = await self._request("GET", "/v1/watches")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return WatchListResponse.model_validate(body)

    async def get_watch(self, watch_id: str) -> WatchState:
        """Get one watch's revalidation state."""
        response = await self._request("GET", f"/v1/watches/{watch_id}")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return WatchState.model_validate(body)

    async def delete_watch(self, watch_id: str) -> None:
        """Stop checking a watch's URL."""
        response = await self._request("DELETE", f"/v1/watches/{watch_id}")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)

    async def capabilities(self) -> CapabilitiesResponse:
        """Service capabilities discovery (GET /v1/capabilities)."""
        response = await self._request("GET", "/v1/capabilities")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return CapabilitiesResponse.model_validate(body)

    async def agent_hints(self) -> AgentHintsResponse:
        """Agent guidance (GET /v1/agent-hints)."""
        response = await self._request("GET", "/v1/agent-hints")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return AgentHintsResponse.model_validate(body)

    async def health(self) -> HealthResponse:
        """Lightweight liveness (GET /health)."""
        response = await self._request("GET", "/health")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return HealthResponse.model_validate(body)

    async def ready(self) -> ReadinessResponse:
        """Crawl capacity/readiness (GET /ready)."""
        response = await self._request("GET", "/ready")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return ReadinessResponse.model_validate(body)

    def sync(self) -> CrawlutronClientSync:
        """A sync client sharing this client's configuration."""
        return CrawlutronClientSync(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
        )


class CrawlutronClientSync(_ClientBase):
    """Blocking typed client mirroring :class:`CrawlutronClient`."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout)
        self._transport = transport
        self._client: httpx.Client | None = None

    def __enter__(self) -> CrawlutronClientSync:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._headers(),
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        client = self._get_client()
        try:
            return client.request(method, path, json=json)
        except httpx.HTTPError as error:
            raise self._transport_error(error) from error

    def crawl(self, payload: CrawlRequest) -> CrawlResponse:
        response = self._request("POST", "/v1/crawl", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body, accepted_if_pending=True)
        return CrawlResponse.model_validate(body)

    def crawl_async(self, payload: CrawlRequest) -> CrawlAccepted:
        request = payload.model_copy(update={"async_mode": True})
        response = self._request("POST", "/v1/crawl", json=_payload_dict(request))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return CrawlAccepted.model_validate(body)

    def get_crawl_result(self, request_id: str) -> CrawlResponse:
        """Retrieve a completed crawl; raises if still accepted (202) or gone (404)."""
        response = self._request("GET", f"/v1/crawl/{request_id}")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body, accepted_if_pending=True)
        return CrawlResponse.model_validate(body)

    def wait_for(
        self,
        request_id: str,
        *,
        poll_interval: float = 1.0,
        max_wait: float | None = None,
    ) -> CrawlResponse:
        elapsed = 0.0
        while True:
            try:
                return self.get_crawl_result(request_id)
            except CrawlutronAcceptedError:
                pass
            if max_wait is not None and elapsed >= max_wait:
                raise CrawlutronTransportError(
                    f"crawl {request_id} did not reach a terminal state within {max_wait}s",
                    status_code=None,
                )
            time.sleep(poll_interval)
            elapsed += poll_interval

    def admit(self, payload: AdmitRequest) -> AdmitResponse:
        response = self._request("POST", "/v1/admit", json=_payload_dict(payload))
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return AdmitResponse.model_validate(body)

    def capabilities(self) -> CapabilitiesResponse:
        response = self._request("GET", "/v1/capabilities")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return CapabilitiesResponse.model_validate(body)

    def health(self) -> HealthResponse:
        response = self._request("GET", "/health")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return HealthResponse.model_validate(body)

    def ready(self) -> ReadinessResponse:
        response = self._request("GET", "/ready")
        body = _json_body(response)
        self._raise_for_status(response.status_code, body)
        return ReadinessResponse.model_validate(body)


__all__ = [
    "CrawlutronClient",
    "CrawlutronClientSync",
]
