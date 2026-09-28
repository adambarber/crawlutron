"""Async harnesses: what an API test is handed, and the pooled-client rebind.

The ``crawl_api`` fixture answers the question every ``/v1/crawl`` test used to
re-ask in its own ``setup`` method: which client am I talking through, what
origin am I talking to, and is the webhook sink mine to read. The sink is the
part that was easy to get wrong -- ``WEBHOOK_RECEIVED`` is one process-global
list in ``helpers.server``, so a test that polls it without clearing it first
can be handed the previous test's delivery and still pass. The clear belongs
with handing out the client, which is what makes the pairing a fixture rather
than a convention.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from tests._timing_harnesses import await_until


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
def crawl_api(async_client, base_url) -> CrawlApi:
    """An ASGI client, the helper server's origin, and a cleared webhook sink."""
    from tests.helpers.server import WEBHOOK_RECEIVED

    WEBHOOK_RECEIVED.clear()
    return CrawlApi(client=async_client, base=base_url)


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
