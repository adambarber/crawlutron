"""API test harnesses shared across the api/ test modules.

Declared support surface (#476): moved verbatim from the sibling test modules
so a test imports these from here, not from another test's internals.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from crawlutron.storage.job_store import JobStore

if TYPE_CHECKING:
    from httpx import AsyncClient

    from crawlutron.api.caller_concurrency import CallerConcurrencyTracker
    from crawlutron.storage.caller_key_store import IssuedKey

#: What ``keyed_quota_client`` yields: client, job store, concurrency tracker, issued key.
type _QuotaClient = tuple[AsyncClient, JobStore, CallerConcurrencyTracker, IssuedKey]

__all__ = [
    "keyed_quota_client",
    "run_with_webhook",
    "spy_on_history_reads",
    "stored_request",
]


def spy_on_history_reads(monkeypatch):
    """Wrap the accumulator readers, recording every full-accumulator read.

    The spy wraps ``JobAccumulators`` (not ``load``) so the real reads still
    run; every caller asserts on the recorded names."""
    from crawlutron.storage.job_store_accumulators import JobAccumulators

    reached: list[str] = []
    for name in ("read_results", "read_visited"):
        real = getattr(JobAccumulators, name)

        def wrapper(self, job_id, *, _name=name, _real=real):
            reached.append(_name)
            return _real(self, job_id)

        monkeypatch.setattr(JobAccumulators, name, wrapper)
    return reached


async def run_with_webhook(request_id, execute_fn, webhook_url, job_store, result_store):
    """One async crawl with a scripted executor and a captured webhook post.

    ``webhook_url`` admits at this harness edge, the same conversion the API
    door performs; delivery sees the target, never a raw string."""
    from crawlutron.api.async_crawl_runner import run_async_crawl
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron.models.webhook_target import WebhookTarget

    target = None if webhook_url is None else WebhookTarget.admit(webhook_url, host_policy=_webhook_policy())
    with patch("crawlutron.api.async_crawl_runner.execute_crawl", new=execute_fn):
        with patch("crawlutron.api.webhooks.post_webhook", new_callable=AsyncMock) as post_webhook:
            await run_async_crawl(
                request_id,
                CrawlRequest.model_validate({"url": "http://example.com/"}),
                target,
                CrawlerSettings.from_mapping(),
                job_store,
                result_store,
                asyncio.Event(),
            )
    return post_webhook


def _webhook_policy():
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.host_policy import HostPolicy

    return HostPolicy.from_settings(CrawlerSettings.from_mapping())


def admitted_webhook(raw: str):
    """An admitted WebhookTarget for tests that pass a target to the runner."""
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.host_policy import HostPolicy
    from crawlutron.models.webhook_target import WebhookTarget

    policy = HostPolicy.from_settings(CrawlerSettings.from_mapping())
    return WebhookTarget.admit(raw, host_policy=policy)


def stored_request(store: JobStore, response):
    """The stored job a 202 crawl response names; the post succeeded."""
    assert response.status_code == 202, response.text
    stored = store.load(response.json()["request_id"])
    assert stored is not None
    return stored


def mount_tracker(client: AsyncClient):
    """Attach the sync-door register to the test client's app and return it."""
    from crawlutron.api.caller_concurrency import CallerConcurrencyTracker

    tracker = CallerConcurrencyTracker()
    transport = client._transport
    # ASGITransport carries the app; assert it rather than import the class here.
    app = getattr(transport, "app", None)
    assert app is not None, "the quota client must ride an ASGI transport"
    app.state.concurrency_tracker = tracker
    return tracker


@contextlib.asynccontextmanager
async def keyed_quota_client(tmp_path, *, max_concurrency: int = 1) -> AsyncGenerator[_QuotaClient, None]:
    """A quota client with a mounted tracker and one keyed caller."""
    from tests.factories.client import quota_client

    async with quota_client(tmp_path) as (client, store, results, keys):
        tracker = mount_tracker(client)
        issued = keys.issue_key("agent-a", max_concurrency=max_concurrency)
        yield client, store, tracker, issued
