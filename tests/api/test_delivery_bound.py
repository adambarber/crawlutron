"""#655: the tracked-delivery set cannot grow without bound in a long-running process."""

from __future__ import annotations

import asyncio

from crawlutron.api import webhooks
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.webhook_target import WebhookTarget
from tests._webhook_doubles import record_webhook_delivery


async def test_delivery_set_is_bounded_under_more_deliveries_than_the_cap(monkeypatch):
    """#655: deliveries beyond the cap are refused (``None``), never spawned to
    accumulate -- behavioral door: ``start_webhook_delivery`` returns a task up
    to the cap and ``None`` past it."""
    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()
    record_webhook_delivery(monkeypatch)

    async def _stall(*_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(webhooks, "post_webhook", _stall)
    target = WebhookTarget.admit("https://webhook.example.com/cb", host_policy=HostPolicy.unrestricted())
    tasks = [webhooks.start_webhook_delivery(target, {"n": i}) for i in range(webhooks.DELIVERIES_CAP + 5)]
    spawned = [t for t in tasks if t is not None]
    refused = [t for t in tasks if t is None]
    assert len(spawned) == webhooks.DELIVERIES_CAP
    assert len(refused) == 5
    for task in spawned:
        task.cancel()
