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

from crawlutron.storage import caller_key_store, result_store
from crawlutron.storage.job_store import JobStatus, JobStore

if TYPE_CHECKING:
    from httpx import AsyncClient

    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
    from crawlutron.storage.caller_key_store import IssuedKey

#: What ``keyed_quota_client`` yields: client, job store, concurrency tracker, issued key.
type _QuotaClient = tuple[AsyncClient, JobStore, CallerConcurrencyTracker, IssuedKey]

__all__ = [
    "assert_crawl_result_served",
    "assert_delete_purges_result",
    "issue_agent_key",
    "isolated_client",
    "isolated_delete_job_client",
    "keyed_quota_client",
    "run_with_webhook",
    "seed_completed_crawl",
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
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron.models.webhook_target import WebhookTarget
    from crawlutron.service.async_crawl_runner import run_async_crawl

    target = None if webhook_url is None else WebhookTarget.admit(webhook_url, host_policy=_webhook_policy())
    with patch("crawlutron.service.async_crawl_runner.execute_crawl", new=execute_fn):
        with patch("crawlutron.service.webhooks.post_webhook", new_callable=AsyncMock) as post_webhook:
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
    from safe_url.host_policy import HostPolicy

    from crawlutron.config.settings import CrawlerSettings

    return HostPolicy.from_settings(CrawlerSettings.from_mapping())


def admitted_webhook(raw: str):
    """An admitted WebhookTarget for tests that pass a target to the runner."""
    from safe_url.host_policy import HostPolicy

    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.webhook_target import WebhookTarget

    policy = HostPolicy.from_settings(CrawlerSettings.from_mapping())
    return WebhookTarget.admit(raw, host_policy=policy)


def stored_request(store: JobStore, response):
    """The stored job a 202 crawl response names; the post succeeded."""
    assert response.status_code == 202, response.text
    stored = store.load(response.json()["request_id"])
    assert stored is not None
    return stored


def issue_agent_key(keys):
    """Issue one key for agent-a; return ``(secret, owner_id, headers)``.

    The sync door variant: the REST tests hold the ``CallerKeyStore`` object
    directly and issue through it, while MCP tests go through the HTTP door.
    """
    issued = keys.issue_key("agent-a")
    secret = issued.secret
    owner = keys.verify_key(secret).key_id
    return secret, owner, {"X-API-Key": secret}


async def seed_completed_crawl(store, results, owner, request_id):
    """Create a completed single-shot job and store a result row keyed by request_id.

    The state a finished async crawl leaves: a terminal job row plus a stored
    result row. Used by the delete-purge tests in both the REST and MCP files.
    """
    store.create_single_shot(request_id, "http://example.com/", {}, owner=owner)
    await store.aset_status(request_id, JobStatus.COMPLETED, expected=JobStatus.PENDING)
    await results.aput(
        request_id,
        {"request_id": request_id, "results": [], "submitted_count": 0, "success": True},
        owner=owner,
    )


async def assert_crawl_result_served(client, request_id, headers):
    """Assert GET /v1/crawl/{request_id} returns 200 with the stored result.

    Pre-fix this still returns 200 after the delete; post-fix it must 404.
    """
    response = await client.get(f"/v1/crawl/{request_id}", headers=headers)
    assert response.status_code == 200, response.text


async def assert_delete_purges_result(client, request_id, headers):
    """DELETE /v1/jobs/{request_id} and assert the result row is purged.

    Deletes the job (expecting 200), then asserts GET /v1/crawl/{request_id}
    returns 404 -- the job's content is no longer served.
    """
    deleted = await client.delete(f"/v1/jobs/{request_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    after = await client.get(f"/v1/crawl/{request_id}", headers=headers)
    assert after.status_code == 404, after.text


@contextlib.asynccontextmanager
async def isolated_delete_job_client(tmp_path, routers):
    """An isolated client with an issued key, for delete-purge tests.

    Yields ``(client, store, results, keys)`` like ``isolated_client``; the
    key is issued for agent-a so tests can scope assertions to one caller.
    """
    async with isolated_client(tmp_path, routers=routers) as (client, store, results, keys):
        yield client, store, results, keys


def mount_tracker(client: AsyncClient):
    """Attach the sync-door register to the test client's app and return it."""
    from tests._app_harnesses import mount_concurrency_tracker

    return mount_concurrency_tracker(client)


@contextlib.asynccontextmanager
async def keyed_quota_client(tmp_path, *, max_concurrency: int = 1) -> AsyncGenerator[_QuotaClient, None]:
    """A quota client with a mounted tracker and one keyed caller."""
    from tests.factories.client import quota_client

    async with quota_client(tmp_path) as (client, store, results, keys):
        tracker = mount_tracker(client)
        issued = keys.issue_key("agent-a", max_concurrency=max_concurrency)
        yield client, store, tracker, issued


@contextlib.asynccontextmanager
async def isolated_client(
    tmp_path, *, routers
) -> AsyncGenerator[
    tuple[
        AsyncClient,
        JobStore,
        result_store.CrawlResultStore,
        caller_key_store.CallerKeyStore,
    ],
    None,
]:
    """An HTTP client over ``routers`` backed by isolated job/result/key stores.

    Yields the client plus the three stores so a test can seed a completed
    crawl's job row and result row, then assert the delete door purges it.
    """
    from tests._app_harnesses import harness_app
    from tests._quota_harness import keyed_client, keyed_overrides, stores

    store, results, keys = stores(tmp_path)
    app = harness_app(*routers.values())
    app.state.job_store = store
    app.state.result_store = results
    overrides = keyed_overrides(app, keys, job_store=store, result_store=results)
    for dependency, factory in overrides.items():
        app.dependency_overrides[dependency] = factory
    async with keyed_client(app, keys=keys) as client:
        try:
            yield client, store, results, keys
        finally:
            store.close()
            results.close()
            keys.close()
