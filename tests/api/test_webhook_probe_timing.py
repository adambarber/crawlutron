"""Timing test: webhook probe must run outside admission.admit to avoid taxing
concurrent admits for the same caller."""

import asyncio
import time

from tests.factories.client import quota_client


async def test_slow_probe_does_not_tax_concurrent_admit(tmp_path, monkeypatch):
    """A slow webhook probe must not delay a concurrent admit for the same caller."""
    probe_started = asyncio.Event()

    async def slow_build(normalized, headers=None):
        probe_started.set()
        await asyncio.sleep(2.0)
        return None

    monkeypatch.setattr("crawlutron.transport.pinned_transport.build_pinned_request_async", slow_build)

    async def fake_execute_crawl(*args, **kwargs):
        return {"results": [], "request_id": "crawl_x", "submitted_count": 0, "policy_schema_version": ""}

    monkeypatch.setattr("crawlutron.service.crawl_executor.execute_crawl", fake_execute_crawl)

    async with quota_client(tmp_path) as (client, _store, _results, keys):
        # max_concurrency=2: a cap of 1 refuses request 2 on quota, proving nothing about probe placement.
        issued = await keys.aissue_key("agent-a", max_concurrency=2)
        headers = {"X-API-Key": issued.secret}
        webhook = "https://webhook.example.com/cb"
        url = "http://example.com/"
        pages = 10
        resp1 = asyncio.create_task(
            client.post(
                "/v1/crawl",
                json={
                    "url": url,
                    "async_mode": True,
                    "policy": {"limits": {"max_pages": pages}},
                    "webhook_url": webhook,
                },
                headers=headers,
            )
        )
        await asyncio.wait_for(probe_started.wait(), timeout=5)
        started = time.monotonic()
        resp2 = await asyncio.wait_for(
            client.post(
                "/v1/crawl",
                json={"url": url, "async_mode": True, "policy": {"limits": {"max_pages": pages}}},
                headers=headers,
            ),
            timeout=6,
        )
        elapsed = time.monotonic() - started

        assert resp2.status_code == 202, resp2.text
        assert elapsed < 1.0, f"concurrent admit waited {elapsed:.2f}s for the slow webhook probe"
        await asyncio.wait_for(resp1, timeout=6)
