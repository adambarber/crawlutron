"""Async harnesses: what an API test is handed, and the pooled-client rebind.

The ``crawl_api`` fixture answers the per-test questions (client, origin,
webhook sink); the sink is process-global, so clearing and draining are its."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests._timing_harnesses import await_until


@dataclass(frozen=True)
class CrawlApi:
    """The client, the origin, and the request payloads rooted at that origin."""

    client: Any
    base: str

    def payload(self, **overrides: Any) -> dict:
        """A crawl request for the helper server's root, with ``overrides`` applied.

        ``make_crawl_payload`` owns the payload's shape, so this adds only the
        origin; a test varies the one key it is about."""
        from tests._fetcher_factories import make_crawl_payload

        return make_crawl_payload(f"{self.base}/", **overrides)

    def batch_payload(self, paths: Sequence[str], **overrides: Any) -> dict:
        """A batch crawl request: ``urls``, and no ``url`` beside it.

        The request model takes exactly one of the two, so a batch payload is a
        different shape, stated once instead of rediscovered via a 422."""
        payload = self.payload(urls=[f"{self.base}{path}" for path in paths], **overrides)
        payload.pop("url")
        return payload


@pytest.fixture
async def crawl_api(async_client, base_url) -> CrawlApi:
    """An ASGI client, the helper server's origin, and a cleared webhook sink.

    Teardown drains the crawls this test started -- a straggler losing its
    terminal CAS leaves later tests waiting forever."""
    from tests.helpers.server import WEBHOOK_RECEIVED

    WEBHOOK_RECEIVED.clear()
    api = CrawlApi(client=async_client, base=base_url)
    try:
        yield api
    finally:
        await _drain_running_crawls()


async def _drain_running_crawls(*, guard: float = 10.0) -> None:
    """Wait for every still-active job to reach terminal, then clear strays.

    Bounded, not open-ended: a genuinely stuck crawl should fail at its own
    assertion, not hold later tests open."""
    from app.main import job_store
    from app.storage.job_store import TERMINAL_STATUSES, JobStatus

    terminal_codes = {status.value for status in TERMINAL_STATUSES}

    def acting() -> list[str]:
        # ``list_jobs`` is the status view, not the state, so this reads the row dict rather than
        # materialising every result payload to ask a question about the status column.
        return [row["job_id"] for row in job_store.list_jobs() if row["status"] not in terminal_codes]

    try:
        await await_until(lambda: not acting(), "a crawl was still active when its test finished")
    except AssertionError:
        # A stuck crawl is reported, not raised: raising here would mask the assertion of the test that
        # actually caused it.
        pass
    # A stale PENDING/RUNNING anonymous row would bleed into the next test's quota; a
    # bounded wait that cannot retire it force-cancels it so none leaks.
    for job_id in acting():
        job_store.set_status(job_id, JobStatus.CANCELLED, expected={JobStatus.PENDING, JobStatus.RUNNING})


async def run_loop_rebind_scenario(get_client, *, guard: float | None = None):
    """Rebind a pooled client onto a foreign loop and let its eager close run.

    The scenario used to end in ``asyncio.sleep`` -- a bet. Every pooled shape
    publishes ``closed``, so the scenario waits for the close it scheduled."""
    first = get_client()
    foreign = asyncio.new_event_loop()
    try:
        first._bound_loop = foreign
        second = get_client()
        kwargs = {} if guard is None else {"guard": guard}
        await await_until(
            lambda: first.closed,
            "the superseded client's eager close never ran",
            **kwargs,
        )
    finally:
        foreign.close()
    return first, second


@contextlib.asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """The one ASGI test-client bootstrap: mount the app and speak HTTP to it."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def issue_key(client: AsyncClient, *, max_pages: int = 0, max_concurrency: int = 0) -> str:
    """Issue one keyed caller through the public keys door; returns its secret."""
    body: dict[str, Any] = {"label": "agent-a"}
    if max_pages:
        body["max_pages"] = max_pages
    if max_concurrency:
        body["max_concurrency"] = max_concurrency
    response = await client.post("/v1/keys", json=body, headers={"X-API-Key": "test-admin-token"})
    assert response.status_code == 201, response.text
    return response.json()["secret"]


def expect_quota_refused(response) -> None:
    """The 429 quota_exceeded shape every quota test reads back."""
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["code"] == "quota_exceeded"


__all__ = [
    "CrawlApi",
    "crawl_api",
    "expect_quota_refused",
    "issue_key",
    "run_loop_rebind_scenario",
    "serve",
]
