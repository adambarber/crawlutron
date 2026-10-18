"""Shared in-process concurrency-bound probing for the sync-crawl and search doors.
Issues #194 and #298 both prove quota by parking one request, 429-ing the second,
then resuming the first, so that choreography lives here once, not in each door.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

_SYNC_RESULT = {"request_id": "sync", "results": [], "submitted_count": 0, "policy_schema_version": ""}


def make_blocking_callable(*, result: Any):
    """Return ``(callable, entered, released)``: the callable signals ``entered``
    then parks until ``released``, both of which the caller uses to steer it."""
    entered = asyncio.Event()
    released = asyncio.Event()

    async def _block(*args, **kwargs):
        entered.set()
        await released.wait()
        return result

    return _block, entered, released


def block_execute_crawl(monkeypatch, module) -> tuple[Callable, asyncio.Event, asyncio.Event]:
    """Stub a module's ``execute_crawl`` with a parked call; drives a sync-crawl probe."""
    blocker, entered, released = make_blocking_callable(result=_SYNC_RESULT)
    monkeypatch.setattr(module, "execute_crawl", blocker)
    return blocker, entered, released


async def assert_second_concurrent_refused(
    client,
    *,
    url: str,
    headers: dict[str, str] | None,
    body: dict[str, Any],
    entered: asyncio.Event,
    released: asyncio.Event,
    first_ok: Iterable[int],
) -> None:
    """Start one POST, let it reach the blocking handler, then confirm the second is 429."""
    first = asyncio.create_task(client.post(url, headers=headers, json=body))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        second = await asyncio.wait_for(client.post(url, headers=headers, json=body), timeout=3)
    except TimeoutError:
        released.set()
        await asyncio.wait_for(first, timeout=5)
        raise AssertionError("second concurrent request was not bounded") from None
    assert second.status_code == 429, second.text
    assert second.json()["detail"]["code"] == "quota_exceeded"
    released.set()
    response = await asyncio.wait_for(first, timeout=5)
    assert response.status_code in first_ok, response.text
