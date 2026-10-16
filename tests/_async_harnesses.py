"""Async harnesses: what an API test is handed, and the pooled-client rebind.

The ``crawl_api`` fixture answers the per-test questions (client, origin,
webhook sink); the sink is process-global, so clearing and draining are its."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from tests._timing_harnesses import await_until


class CountingSemaphore:
    """A fixed-width semaphore that records the peak number of holders.

    Replaces the runner's semaphore so a test asserts the BOUND, not
    throughput: "N complete" proves nothing under the limit."""

    def __init__(self, width: int) -> None:
        self._semaphore = asyncio.Semaphore(width)
        self._holders = 0
        self.peak_holders = 0

    async def __aenter__(self) -> "CountingSemaphore":
        await self._semaphore.acquire()
        self._holders += 1
        self.peak_holders = max(self.peak_holders, self._holders)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._holders -= 1
        self._semaphore.release()


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
    """Wait for every job still ``RUNNING`` to leave it, or say which one did not.

    Bounded, not open-ended: a genuinely stuck crawl should fail at its own
    assertion, not hold later tests open."""
    from app.main import job_store
    from app.storage.job_store import JobStatus

    def running() -> list[str]:
        # ``list_jobs`` is the status view, not the state, so this reads the row dict rather than
        # materialising every result payload to ask a question about the status column.
        return [row["job_id"] for row in job_store.list_jobs() if row["status"] == JobStatus.RUNNING.value]

    try:
        await await_until(running, "a crawl was still RUNNING when its test finished")
    except AssertionError:
        # A stuck crawl is reported, not raised: raising here would mask the assertion of the test that
        # actually caused it.
        pass


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


__all__ = ["CrawlApi", "crawl_api", "run_loop_rebind_scenario"]
