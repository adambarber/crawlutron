"""Async harnesses: what an API test is handed, and the pooled-client rebind.

The ``crawl_api`` fixture answers the question every ``/v1/crawl`` test used to
re-ask in its own ``setup`` method: which client am I talking through, what
origin am I talking to, and is the webhook sink mine to read. The sink is the
part that was easy to get wrong -- ``WEBHOOK_RECEIVED`` is one process-global
list in ``helpers.server``, so a test that polls it without clearing it first
can be handed the previous test's delivery and still pass. The clear belongs
with handing out the client, which is what makes the pairing a fixture rather
than a convention. The teardown drain is the other half of that pairing: the
fixture owns the crawls it hands out, so it is what stops them.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from tests._timing_harnesses import await_until


class CountingSemaphore:
    """A fixed-width semaphore that records the peak number of holders.

    Replaces the runner's semaphore so a test can assert the *bound* rather
    than infer it. A test that only checks that N concurrent submissions all
    complete proves nothing about the limit when N is under it -- three
    submissions against a limit of four exercises no queueing, and an
    implementation with no bound at all passes.

    The peak is also what makes the bound falsifiable. It reads 1 only if the
    crawler entered this object, so deleting the ``async with`` in
    ``run_async_crawl`` drops it to 0, and a width that is not 1 pushes it
    above 1. Neither needs a slow page or a wall clock to observe.
    """

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
        """A crawl request for the helper server's root, with `overrides` applied.

        ``make_crawl_payload`` owns the payload's shape -- it is the funnel for
        that -- so this adds only the origin. A test varies the one key it is
        about and never restates the URL.
        """
        from tests._fetcher_factories import make_crawl_payload

        return make_crawl_payload(f"{self.base}/", **overrides)

    def batch_payload(self, paths: Sequence[str], **overrides: Any) -> dict:
        """A batch crawl request: ``urls``, and no ``url`` beside it.

        The request model takes exactly one of the two (see
        ``_require_exactly_one_url_source``), so a batch payload is a different
        shape from a single-page one rather than the same payload with a key
        added. Stating that here is what keeps a second test from rediscovering
        it by getting a 422.
        """
        payload = self.payload(urls=[f"{self.base}{path}" for path in paths], **overrides)
        payload.pop("url")
        return payload


@pytest.fixture
async def crawl_api(async_client, base_url) -> CrawlApi:
    """An ASGI client, the helper server's origin, and a cleared webhook sink.

    Teardown drains the crawls this test started, because a crawl that outlives
    its test is a defect that lands on the *next* one. The app's shutdown event
    is process-global and the per-test teardown replaces it; a straggler still
    running when that flips is paused, its terminal write then loses the
    compare-and-set against ``PAUSED``, and the results it will never publish
    leave a later test waiting for a completion that cannot arrive. A test that
    only asserts the 202 still starts a crawl, so this belongs to the fixture
    rather than to each test that remembered.
    """
    from tests.helpers.server import WEBHOOK_RECEIVED

    WEBHOOK_RECEIVED.clear()
    api = CrawlApi(client=async_client, base=base_url)
    try:
        yield api
    finally:
        await _drain_running_crawls()


async def _drain_running_crawls(*, guard: float = 10.0) -> None:
    """Wait for every job still ``RUNNING`` to leave it, or say which one did not.

    Bounded rather than open-ended on purpose: a crawl that is genuinely stuck
    should fail at its own assertion, not hold every later test open until the
    suite's hang guard fires somewhere unrelated.
    """
    from app.main import job_store
    from app.storage.job_store import JobStatus

    def running() -> list[str]:
        # ``list_jobs`` is the status view, not the state, so this reads the
        # row dict rather than materialising every result payload to ask a
        # question about the status column.
        return [row["job_id"] for row in job_store.list_jobs() if row["status"] == JobStatus.RUNNING.value]

    try:
        await await_until(running, "a crawl was still RUNNING when its test finished")
    except AssertionError:
        # A stuck crawl is reported, not raised: raising here would mask the
        # assertion of the test that actually caused it.
        pass


async def run_loop_rebind_scenario(get_client, *, guard: float | None = None):
    """Rebind a pooled client onto a foreign loop and let its eager close run.

    The scenario used to end in ``await asyncio.sleep(delay)`` -- a fixed
    pause whose only job was to give the teardown the code had just scheduled a
    chance to run. That is a bet, not an assertion: on a loaded box the close
    may not have started yet, and the test still passed because it only checked
    the flag afterwards. Every pooled client shape in the suite publishes
    ``closed``, so the wait is on *that*: the scenario returns once the close
    the code scheduled has actually completed, and returns immediately if it
    never was scheduled at all.
    """
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
