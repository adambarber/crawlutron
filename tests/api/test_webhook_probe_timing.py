"""Ordering proof: the webhook probe runs before admission reserves the caller's slot.

A wall-clock assertion cannot state this: with spare concurrency nothing
contends, and with none the second request is refused on quota.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import patch

from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from tests.factories.client import quota_client

_reserve = cast("Callable[..., Awaitable[object]]", CallerConcurrencyTracker.reserve)


async def test_webhook_probe_completes_before_admission_is_entered(tmp_path, monkeypatch):
    """The probe runs before admission reserves the slot, so it cannot hold the lock."""
    events: list[str] = []

    async def slow_probe(webhook_url: str, *, host_policy: object = None) -> None:
        await asyncio.sleep(0.05)
        events.append("probe")

    async def recording_reserve(self: object, *args: object, **kwargs: object) -> object:
        events.append("reserve")
        return await _reserve(self, *args, **kwargs)

    monkeypatch.setattr("crawlutron.service.webhooks.validate_webhook_url_async", slow_probe)

    with patch.object(CallerConcurrencyTracker, "reserve", recording_reserve):
        async with quota_client(tmp_path) as (client, _store, _results, keys):
            issued = await keys.aissue_key("agent-a", max_concurrency=2)
            response = await client.post(
                "/v1/crawl",
                json={
                    "url": "http://example.com/",
                    "async_mode": True,
                    "policy": {"limits": {"max_pages": 10}},
                    "webhook_url": "https://webhook.example.com/cb",
                },
                headers={"X-API-Key": issued.secret},
            )

    assert response.status_code == 202, response.text
    assert events[:2] == ["probe", "reserve"], (
        "the webhook probe must finish before admission reserves the caller's "
        f"slot; observed order: {events[:4]}"
    )
