"""Shared in-process concurrency-bound probing for the sync-crawl and search doors.
Issues #194 and #298 both prove quota by parking one request, 429-ing the second,
then resuming the first, so that choreography lives here once, not in each door.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import NamedTuple

_SYNC_RESULT = {"request_id": "sync", "results": [], "submitted_count": 0, "policy_schema_version": ""}


def sync_crawl_blocker() -> BlockHandle:
    """A parked sync-crawl engine seam (the shared _SYNC_RESULT shape)."""
    return make_blocking_callable(result=_SYNC_RESULT)


def block_sync_crawl(monkeypatch) -> BlockHandle:
    """Stub the crawl_endpoint engine seam with a parked sync-crawl call."""
    from crawlutron.api import crawl_endpoint

    handle = sync_crawl_blocker()
    monkeypatch.setattr(crawl_endpoint, "execute_crawl", handle.blocker)
    return handle


async def crawl_then_second_refused(
    client,
    *,
    headers: dict[str, str] | None,
    entered: asyncio.Event,
    released: asyncio.Event,
    second_url: str | None = None,
    second_body: dict[str, object] | None = None,
) -> None:
    """Park one sync crawl, then 429 the second request (same or cross door)."""
    await assert_second_concurrent_refused(
        client,
        url="/v1/crawl",
        headers=headers,
        body={"url": "http://example.com/"},
        entered=entered,
        released=released,
        first_ok=(200, 201),
        second_url=second_url,
        second_body=second_body,
    )


class BlockHandle(NamedTuple):
    """The parked call and the two events that steer it."""

    blocker: Callable[..., object]
    entered: asyncio.Event
    released: asyncio.Event


def make_blocking_callable(*, result: object) -> BlockHandle:
    """Return ``(callable, entered, released)``: the callable signals ``entered``
    then parks until ``released``, both of which the caller uses to steer it."""
    entered = asyncio.Event()
    released = asyncio.Event()

    async def _block(*args, **kwargs):
        entered.set()
        await released.wait()
        return result

    return BlockHandle(_block, entered, released)


def block_execute_crawl(monkeypatch, module) -> BlockHandle:
    """Stub a module's ``execute_crawl`` with a parked call; drives a sync-crawl probe."""
    blocker, entered, released = sync_crawl_blocker()
    monkeypatch.setattr(module, "execute_crawl", blocker)
    return BlockHandle(blocker, entered, released)


async def assert_second_concurrent_refused(
    client,
    *,
    url: str,
    headers: dict[str, str] | None,
    body: dict[str, object],
    entered: asyncio.Event,
    released: asyncio.Event,
    first_ok: Iterable[int],
    second_url: str | None = None,
    second_body: dict[str, object] | None = None,
) -> None:
    """Start one POST, let it reach the blocking handler, then confirm the second is 429.

    The second request may target a different door (``second_url``/``second_body``):
    the cross-door probes pin that both doors read the SAME register."""
    first = asyncio.create_task(client.post(url, headers=headers, json=body))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        second = await asyncio.wait_for(
            client.post(second_url or url, headers=headers, json=second_body or body), timeout=3
        )
    except TimeoutError:
        released.set()
        await asyncio.wait_for(first, timeout=5)
        raise AssertionError("second concurrent request was not bounded") from None
    assert second.status_code == 429, second.text
    assert second.json()["detail"]["code"] == "quota_exceeded"
    released.set()
    response = await asyncio.wait_for(first, timeout=5)
    assert response.status_code in first_ok, response.text
