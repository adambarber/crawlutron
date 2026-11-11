"""Concurrency-quota TOCTOU on the async doors (#464).

The count->insert window let two concurrent posts pass a quota of one.
"""

import asyncio
import contextlib

from tests._api_harnesses import keyed_quota_client as _keyedquota_client
from tests._timing_harnesses import await_until


def _assert_quota_outcome(responses, *, overlapped: bool, store, tracker, issued) -> None:
    """One 202, one 429 quota_exceeded, no overlap, and no leaked slot."""
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
        assert response.json()["code"] == "quota_exceeded"
    assert tracker.sync_in_flight(issued.key_id) == 0, "a refused or handed-off slot was leaked"
    assert store.active_count_for_caller(issued.key_id) == 1


def _two_concurrent_posts(client, headers):
    """Two simultaneous async-mode posts racing for one quota slot."""
    return [
        asyncio.create_task(
            client.post(
                "/v1/crawl",
                headers=headers,
                json={"url": "http://example.com/", "async_mode": True},
            )
        )
        for _ in range(2)
    ]


def _instrument(store, original, pinned):
    """Count-overlap detector plus a pinned crawl; returns (spy, overlapped getter)."""
    state = {"counting": False, "overlapped": False}

    async def instrumented_count(caller_id):
        if state["counting"]:
            state["overlapped"] = True
        state["counting"] = True
        try:
            # Long enough that the loop definitely runs the other admit
            # while this count is open; a serialized admit cannot overlap.
            await asyncio.sleep(0.01)
            return await original(caller_id)
        finally:
            state["counting"] = False

    async def pinned_crawl(*args, **kwargs):
        # Hold the first admit's row active through the second admit: a job
        # that dies first would release its slot legitimately.
        await pinned.wait()
        return {}

    return instrumented_count, pinned_crawl, state


async def test_two_concurrent_async_posts_cannot_both_pass_a_quota_of_one(tmp_path, monkeypatch):
    """No two admits may count concurrently: the admit lock spans count->insert."""
    async with _keyedquota_client(tmp_path) as (client, store, tracker, issued):
        pinned = asyncio.Event()
        count_spy, pinned_crawl, state = _instrument(store, store.aactive_count_for_caller, pinned)

        monkeypatch.setattr(store, "aactive_count_for_caller", count_spy)
        monkeypatch.setattr("crawlutron.api.async_crawl_runner.execute_crawl", pinned_crawl)

        posts = _two_concurrent_posts(client, {"X-API-Key": issued.secret})
        responses = [await asyncio.wait_for(t, timeout=10) for t in posts]
        try:
            _assert_quota_outcome(
                responses, overlapped=state["overlapped"], store=store, tracker=tracker, issued=issued
            )
        finally:
            # Always unblock the runner: a failed assertion must not leave it
            # parked on the event, holding the webhook semaphore at teardown.
            pinned.set()
            with contextlib.suppress(AssertionError):
                await await_until(
                    lambda: store.active_count_for_caller(issued.key_id) == 0,
                    "the pinned async crawl never reached terminal",
                )
