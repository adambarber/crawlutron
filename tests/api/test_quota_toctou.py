"""Concurrency-quota TOCTOU on the async doors (#464).

The count->insert window let two concurrent posts pass a quota of one.
"""

import asyncio

from tests.api.test_caller_quota_binding import _keyedquota_client


async def test_two_concurrent_async_posts_cannot_both_pass_a_quota_of_one(tmp_path, monkeypatch):
    """No two admits may count concurrently: the admit lock spans count->insert."""
    async with _keyedquota_client(tmp_path) as (client, store, tracker, issued):
        original = store.aactive_count_for_caller
        counting = False
        overlapped = False
        headers = {"X-API-Key": issued.secret}

        async def instrumented_count(caller_id):
            nonlocal counting, overlapped
            if counting:
                overlapped = True
            counting = True
            try:
                # Long enough that the loop definitely runs the other admit
                # while this count is open; a serialized admit cannot overlap.
                await asyncio.sleep(0.01)
                return await original(caller_id)
            finally:
                counting = False

        monkeypatch.setattr(store, "aactive_count_for_caller", instrumented_count)

        tasks = [
            asyncio.create_task(
                client.post(
                    "/v1/crawl",
                    headers=headers,
                    json={"url": "http://example.com/", "async_mode": True},
                )
            )
            for _ in range(2)
        ]
        responses = [await asyncio.wait_for(t, timeout=10) for t in tasks]

    accepted = [r for r in responses if r.status_code == 202]
    refused = [r for r in responses if r.status_code == 429]
    assert not overlapped, (
        "two admits counted the caller's active rows concurrently: the "
        "count->insert window admitted a request past the quota"
    )
    assert len(accepted) == 1, (
        f"expected exactly one 202 for a quota-1 caller, got {len(accepted)}: "
        "concurrent requests were admitted past the quota"
    )
    assert len(refused) == 1
    for response in refused:
        assert response.json()["detail"]["code"] == "quota_exceeded"
    assert tracker.sync_in_flight(issued.key_id) == 0, "a refused or handed-off slot was leaked"
    assert store.active_count_for_caller(issued.key_id) == 1
