"""Async harnesses: what an API test is handed, and the pooled-client rebind.

The ``crawl_api`` fixture answers the per-test questions (client, origin,
webhook sink); the sink is process-global, so clearing and draining are its."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crawlutron.models.json_value import JsonValue
from crawlutron.storage.safe_db import allow_on_loop
from tests._timing_harnesses import await_until


@dataclass(frozen=True)
class CrawlApi:
    """The client, the origin, and the request payloads rooted at that origin."""

    client: AsyncClient
    base: str

    def payload(self, *, async_mode: bool = False, webhook_url: str | None = None) -> dict[str, JsonValue]:
        """A crawl request for the helper server's root, with the async keys applied.

        ``make_crawl_payload`` owns the payload's shape, so this adds only the
        origin and the two async keys; a test varies the one key it is about."""
        from tests._fetcher_factories import make_crawl_payload

        return make_crawl_payload(f"{self.base}/", async_mode=async_mode, webhook_url=webhook_url)

    def batch_payload(
        self, paths: Sequence[str], *, async_mode: bool = False, webhook_url: str | None = None
    ) -> dict[str, JsonValue]:
        """A batch crawl request: ``urls``, and no ``url`` beside it.

        The request model takes exactly one of the two, so a batch payload is a
        different shape, stated once instead of rediscovered via a 422."""
        payload = self.payload(async_mode=async_mode, webhook_url=webhook_url)
        payload["urls"] = [f"{self.base}{path}" for path in paths]
        payload.pop("url")
        return payload


@pytest.fixture
async def crawl_api(async_client, base_url) -> AsyncGenerator[CrawlApi]:
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
    """Wait for active jobs, then retire their tasks through the door.

    A status write alone leaves the crawl's connections running into teardown.
    """
    import asyncio

    from crawlutron.service import job_lifecycle
    from crawlutron.storage.job_store import TERMINAL_STATUSES, JobStatus
    from crawlutron_admit import Count, Duration
    from crawlutron_admit.task_scope import Grace, NamedRegistry, ScopeRefused, TaskScope
    from tests._app_factory import service_app

    job_store = service_app().state.job_store
    terminal_codes = {status.value for status in TERMINAL_STATUSES}
    grace = Grace(0.05)
    budget = 1.0

    async def acting() -> list[str]:
        # ``list_jobs`` is the status view, not the state, so this reads the row dict rather than
        # materialising every result payload to ask a question about the status column.
        return [row["job_id"] for row in await job_store.alist_jobs() if row["status"] not in terminal_codes]

    async def drained() -> bool:
        await asyncio.sleep(0.005)
        return not await acting()

    try:
        await asyncio.wait_for(drained(), timeout=guard)
    except (AssertionError, TimeoutError):
        # A stuck crawl is reported, not raised: raising here would mask the assertion of the test that
        # actually caused it.
        pass

    # A stale row bleeds into the next test's quota, so the terminal write stays
    # as a backstop; the task itself is cancelled and awaited through the door.
    pending = frozenset({JobStatus.PENDING, JobStatus.RUNNING})
    clock = asyncio.get_running_loop().time
    for job_id in await acting():
        deadline = clock() + budget
        while clock() < deadline:
            owned = job_lifecycle.tracked_task(job_id)
            if owned is None:
                break
            scope = TaskScope.process(
                registry=NamedRegistry("test:crawl-drain", Count(1), Duration(0.25)),
                grace=grace,
            )
            try:
                await scope.stop(scope.adopt(owned), grace)
            except ScopeRefused:
                # A task swallowing one cancellation past the grace gets a
                # fresh handle and another bounded stop within the budget.
                continue
            break
        with allow_on_loop():
            job_store.set_status(job_id, JobStatus.CANCELLED, expected=pending)


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
async def serve(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """The one ASGI test-client bootstrap: mount the app and speak HTTP to it."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def issue_key(client: AsyncClient, *, max_pages: int = 0, max_concurrency: int = 0) -> str:
    """Issue one keyed caller through the public keys door; returns its secret."""
    body: dict[str, JsonValue] = {"label": "agent-a"}
    if max_pages:
        body["max_pages"] = max_pages
    if max_concurrency:
        body["max_concurrency"] = max_concurrency
    response = await client.post("/v1/keys", json=body, headers={"X-API-Key": "test-admin-token"})
    assert response.status_code == 201, response.text
    return response.json()["secret"]


def expect_quota_refused(response) -> None:
    """The 429 quota_exceeded refusal in the production error envelope."""
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "quota_exceeded"


__all__ = [
    "CrawlApi",
    "crawl_api",
    "expect_quota_refused",
    "issue_key",
    "run_loop_rebind_scenario",
    "serve",
]
