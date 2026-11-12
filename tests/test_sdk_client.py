"""Hermetic unit tests for the SDK client over httpx.MockTransport (no server)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest

from crawlutron_sdk import (
    CrawlutronBadRequestError,
    CrawlutronClient,
    CrawlutronClientClosedRequestError,
    CrawlutronClientSync,
    CrawlutronConflictError,
    CrawlutronUnauthorizedError,
)
from crawlutron_sdk.errors import (
    CrawlutronAcceptedError,
    CrawlutronBadGatewayError,
    CrawlutronError,
    CrawlutronForbiddenError,
    CrawlutronInternalServerError,
    CrawlutronNotFoundError,
    CrawlutronServiceUnavailableError,
    CrawlutronTooManyRequestsError,
    CrawlutronTransportError,
    CrawlutronUnprocessableEntityError,
)
from crawlutron_sdk.models import AdmitRequest, CrawlRequest, CrawlResponse

_CRAWL_BODY = {
    "request_id": "abc",
    "submitted_count": 1,
    "policy_schema_version": "1",
    "success": True,
    "results": [],
}


def _client(handler) -> CrawlutronClient:
    return CrawlutronClient(transport=httpx.MockTransport(handler), base_url="http://test")


def _sync_client(handler) -> CrawlutronClientSync:
    return CrawlutronClientSync(transport=httpx.MockTransport(handler), base_url="http://test")


@asynccontextmanager
async def _served(handler) -> AsyncGenerator[CrawlutronClient, None]:
    client = _client(handler)
    try:
        yield client
    finally:
        await client.aclose()


@asynccontextmanager
async def _served_sync(handler) -> AsyncGenerator[CrawlutronClientSync, None]:
    client = _sync_client(handler)
    try:
        yield client
    finally:
        client.close()


def _crawl_200(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.path == "/v1/crawl"
    return httpx.Response(200, json=_CRAWL_BODY)


def _accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        202,
        json={"status": "accepted", "request_id": "abc", "status_url": "/v1/crawl/abc"},
    )


def _sequential_handler(
    iterator: Iterator[int],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        status = next(iterator)
        if status == 200:
            return httpx.Response(200, json=_CRAWL_BODY)
        return _accepted(request)

    return handler


@pytest.mark.asyncio
async def test_crawl_returns_response():
    async with _served(_crawl_200) as client:
        response = await client.crawl(CrawlRequest(url="https://example.com/"))
    assert isinstance(response, CrawlResponse)
    assert response.request_id == "abc"


@pytest.mark.asyncio
async def test_crawl_async_returns_accepted():
    async with _served(_accepted) as client:
        accepted = await client.crawl_async(CrawlRequest(url="https://example.com/"))
    assert accepted.request_id == "abc"
    assert accepted.status_url == "/v1/crawl/abc"


@pytest.mark.asyncio
async def test_get_crawl_result_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/crawl/abc"
        return httpx.Response(200, json=_CRAWL_BODY)

    async with _served(handler) as client:
        response = await client.get_crawl_result("abc")
    assert response.request_id == "abc"


@pytest.mark.asyncio
async def test_get_crawl_result_202_raises_accepted():
    async with _served(_accepted) as client:
        with pytest.raises(CrawlutronAcceptedError) as excinfo:
            await client.get_crawl_result("abc")
    assert excinfo.value.status_code == 202


@pytest.mark.asyncio
async def test_get_crawl_result_404_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "not_found", "message": "gone", "details": {}})

    async with _served(handler) as client:
        with pytest.raises(CrawlutronNotFoundError) as excinfo:
            await client.get_crawl_result("missing")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_wait_for_polls_then_succeeds():
    states = iter([202, 202, 200])

    async with _served(_sequential_handler(states)) as client:
        response = await client.wait_for("abc", poll_interval=0, max_wait=5)
    assert response.request_id == "abc"


@pytest.mark.asyncio
async def test_wait_for_timeout_raises_transport_error():
    async with _served(_accepted) as client:
        with pytest.raises(CrawlutronTransportError):
            await client.wait_for("abc", poll_interval=0.05, max_wait=0.15)


@pytest.mark.asyncio
async def test_admit_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admit"
        return httpx.Response(200, json={"admitted": True, "checks": {}})

    async with _served(handler) as client:
        verdict = await client.admit(AdmitRequest(url="https://example.com/"))
    assert verdict.admitted is True


@pytest.mark.asyncio
async def test_api_key_is_sent_as_header():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"admitted": True, "checks": {}})

    client = CrawlutronClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        api_key="secret-key",
    )
    try:
        await client.admit(AdmitRequest(url="https://example.com/"))
    finally:
        await client.aclose()
    assert captured[0].headers.get("X-API-Key") == "secret-key"


@pytest.mark.asyncio
async def test_server_errors_map_to_typed_errors():
    cases = {
        400: CrawlutronBadRequestError,
        409: CrawlutronConflictError,
        499: CrawlutronClientClosedRequestError,
        401: CrawlutronUnauthorizedError,
        403: CrawlutronForbiddenError,
        429: CrawlutronTooManyRequestsError,
        500: CrawlutronInternalServerError,
        502: CrawlutronBadGatewayError,
        503: CrawlutronServiceUnavailableError,
        422: CrawlutronUnprocessableEntityError,
    }
    for status, klass in cases.items():

        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json={"code": "c", "message": "m", "details": {}})

        async with _served(handler) as client:
            with pytest.raises(klass):
                await client.admit(AdmitRequest(url="https://example.com/"))


@pytest.mark.asyncio
async def test_unmapped_status_falls_to_generic_base_error():
    """Boundary: a status with no typed class lands on the generic base."""
    _ = CrawlutronError  # import guard keeps the base in scope for the assert

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"code": "teapot", "message": "m", "details": {}})

    async with _served(handler) as client:
        with pytest.raises(CrawlutronError) as excinfo:
            await client.admit(AdmitRequest(url="https://example.com/"))
    assert type(excinfo.value) is CrawlutronError


@pytest.mark.asyncio
async def test_transport_failure_raises_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _served(handler) as client:
        with pytest.raises(CrawlutronTransportError):
            await client.admit(AdmitRequest(url="https://example.com/"))


@pytest.mark.asyncio
async def test_sync_get_crawl_result_202_raises_accepted():
    async with _served_sync(_accepted) as client:
        with pytest.raises(CrawlutronAcceptedError) as excinfo:
            client.get_crawl_result("abc")
    assert excinfo.value.status_code == 202


@pytest.mark.asyncio
async def test_sync_crawl_202_raises_accepted():
    """Sync crawl mirrors async: a 202 (async-mode body) is an accepted error."""
    async with _served_sync(_accepted) as client:
        with pytest.raises(CrawlutronAcceptedError) as excinfo:
            client.crawl(CrawlRequest(url="https://example.com/"))
    assert excinfo.value.status_code == 202


@pytest.mark.asyncio
async def test_crawl_202_parity_between_transports():
    """The same 202 wire event raises the SAME typed class on both clients."""
    async with _served(_accepted) as async_client:
        with pytest.raises(CrawlutronAcceptedError):
            await async_client.crawl(CrawlRequest(url="https://example.com/"))
    async with _served_sync(_accepted) as sync_client:
        with pytest.raises(CrawlutronAcceptedError):
            sync_client.crawl(CrawlRequest(url="https://example.com/"))


@pytest.mark.asyncio
async def test_sync_wait_for_polls_then_succeeds():
    states = iter([202, 202, 200])

    async with _served_sync(_sequential_handler(states)) as client:
        response = client.wait_for("abc", poll_interval=0, max_wait=5)
    assert response.request_id == "abc"


def test_sync_client_crawl():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CRAWL_BODY)

    client = _sync_client(handler)
    try:
        response = client.crawl(CrawlRequest(url="https://example.com/"))
    finally:
        client.close()
    assert response.request_id == "abc"


# Async-only surfaces the sync twin cannot mirror. Derived, not hand-listed per
# the #876 invariant-scope doctrine; each name is a live `dir()` disclosure.
_ASYNC_ONLY_SUBSET = frozenset(
    {
        "aclose",  # async client cleanup; sync exposes its blocking twin `close`.
        "crawl_stream",  # async generator; no sync form exists.
        "sync",  # async->sync factory; meaningless on the sync client.
    }
)


def _public_methods(cls: type) -> frozenset[str]:
    """The live public-method surface of a client class (underscore-free)."""
    return frozenset(name for name in dir(cls) if not name.startswith("_"))


def test_sync_client_mirrors_every_async_public_method():
    """The lock: every public async method has a same-named sync twin."""
    async_public = _public_methods(CrawlutronClient)
    sync_public = _public_methods(CrawlutronClientSync)
    missing = async_public - sync_public - _ASYNC_ONLY_SUBSET
    assert not missing, f"sync client missing async-twins: {sorted(missing)}"


def test_sync_client_adds_no_unmirrored_public_method():
    """The mirror holds both ways: sync public methods are async twins or `close`."""
    async_public = _public_methods(CrawlutronClient) | _ASYNC_ONLY_SUBSET
    sync_public = _public_methods(CrawlutronClientSync) - {"close"}
    extra = sync_public - async_public
    assert not extra, f"sync client has unmirrored public methods: {sorted(extra)}"


@pytest.mark.asyncio
async def test_crawl_stream_yields_response():
    calls = iter([202, 200])

    async with _served(_sequential_handler(calls)) as client:
        results = [r async for r in client.crawl_stream(CrawlRequest(url="https://example.com/"))]
    assert len(results) == 1
    assert results[0].request_id == "abc"
