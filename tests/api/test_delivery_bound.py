"""#655: the tracked-delivery set cannot grow without bound in a long-running process."""

from __future__ import annotations

import asyncio

from crawlutron.api import webhooks
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.webhook_target import WebhookTarget
from tests._webhook_doubles import record_webhook_delivery


async def test_delivery_set_is_bounded_under_more_deliveries_than_the_cap(monkeypatch):
    """#655: more tracked deliveries than the cap must leave the set at the cap --
    excess tasks are refused (best-effort: logged and not spawned), never accumulated."""
    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()
    record_webhook_delivery(monkeypatch)

    async def _stall(*_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(webhooks, "post_webhook", _stall)
    target = WebhookTarget.admit("https://webhook.example.com/cb", host_policy=HostPolicy.unrestricted())
    tasks = [webhooks.start_webhook_delivery(target, {"n": i}) for i in range(2000)]
    await asyncio.sleep(0)
    assert webhooks.tracked_delivery_count() <= webhooks.DELIVERIES_CAP
    for task in tasks:
        if task is not None:
            task.cancel()
