"""Webhook validation and delivery behavior.

Covers failure-webhook delivery and the requirement that webhook URL
validation and pinned delivery resolve DNS off the event loop.
"""

import asyncio
import socket
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.async_crawl_runner import run_async_crawl
from app.crawl.result_store import CrawlResultStore
from app.validation import HostPolicy
from tests._assertions import block_private_targets, host_resolves_to
from tests._webhook_doubles import record_webhook_delivery


def _spy_resolver_threads(monkeypatch) -> list[threading.Thread]:
    """Record the thread every ``socket.getaddrinfo`` call runs on.

    Shared by the validation and delivery offload tests: both stage the same
    spy (a public-IP answer plus the thread capture) and then assert against
    the recorded threads themselves, so the staging exists in one place and
    the two tests can only differ in what they drive.
    """
    resolver_threads: list[threading.Thread] = []

    def spy_getaddrinfo(host, *args, **kwargs):
        resolver_threads.append(threading.current_thread())
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", spy_getaddrinfo)
    return resolver_threads


class TestAsyncCrawlFailureWebhookDelivery:
    """the async-crawl FAILURE webhook must actually be delivered.

    ``await coro().model_dump()`` parses as ``await (coro().model_dump())``:
    ``model_dump()`` runs on the coroutine object (TypeError) and the adjacent
    ``except Exception: pass`` swallows it, so ``post_webhook`` never runs and
    the coroutine is never awaited.
    """

    async def test_failed_async_crawl_delivers_failure_webhook(self, job_store, tmp_path):
        request_id = "crawl_c3_failure"
        job_store.create_single_shot(request_id, "http://example.com/", {"url": "http://example.com/"})

        webhook_url = "http://webhook.example.com/cb"
        result_store = CrawlResultStore(tmp_path / "results.db")

        async def _boom(*args, **kwargs):
            raise RuntimeError("crawl blew up")

        with patch("app.api.async_crawl_runner.execute_crawl", side_effect=_boom):
            with patch("app.api.webhooks.post_webhook", new_callable=AsyncMock) as post_webhook:
                await run_async_crawl(
                    request_id,
                    {"url": "http://example.com/"},
                    webhook_url,
                    MagicMock(),
                    job_store,
                    result_store,
                    asyncio.Event(),
                )

        # The failure webhook must be delivered exactly once.
        assert post_webhook.await_count == 1, (
            f"post_webhook awaited {post_webhook.await_count} times; the failure "
            "webhook is silently undelivered"
        )
        assert post_webhook.await_args is not None, "the webhook was awaited; the call is recorded"
        delivered_url, payload = post_webhook.await_args.args
        assert delivered_url == webhook_url, delivered_url
        # Payload must be a real UnifiedCrawlResponse shape, not a coroutine.
        assert isinstance(payload, dict), f"payload must be a dict, got {type(payload)}"
        assert payload["request_id"] == request_id, payload
        assert payload["success"] is False, payload
        assert isinstance(payload["results"], list), payload


class TestWebhookValidationRunsOffLoop:
    """``validate_webhook_url`` calls ``SafeUrl.parse(pin=True)`` ->
    synchronous ``socket.getaddrinfo`` on the event loop. A caller-chosen
    webhook hostname with a slow/blackholed resolver therefore stalls the
    whole FastAPI loop. Crawl targets already offload the identical resolution
    via ``_build_pinned_request_async`` (a dedicated dns-resolve thread pool);
    webhook validation must reuse that off-thread path, not a second one.

    D3 is the delivery-path twin: ``deliver_pinned_webhook`` re-parses through
    the same chokepoint to close the validation->delivery TOCTOU, and that
    parse ran inline too -- offloading validation while leaving delivery on the
    loop would still stall every webhook callback.
    """

    async def test_post_webhook_validates_off_the_event_loop(self, monkeypatch):
        from app.api.webhooks import post_webhook

        block_private_targets(monkeypatch)
        resolver_threads = _spy_resolver_threads(monkeypatch)
        # Neutralize delivery so the ONLY resolution is the validation parse.
        monkeypatch.setattr("app.api.webhooks.deliver_pinned_webhook", AsyncMock())

        await post_webhook(
            "http://public.example.com/cb",
            {"results": []},
            host_policy=HostPolicy.unrestricted(),
        )

        assert resolver_threads, "socket.getaddrinfo was never called during validation"
        loop_thread = threading.current_thread()
        for thread in resolver_threads:
            assert thread is not loop_thread, (
                f"validation resolved DNS on the event-loop thread {thread.name!r}; "
                "it must run on a dns-resolve worker thread"
            )
            assert thread.name.startswith("dns-resolve"), (
                f"validation did not reuse the dns-resolve pool (ran on {thread.name!r})"
            )

    async def test_deliver_pinned_webhook_parses_off_the_event_loop(self, monkeypatch):
        """D3: the delivery parse must also leave the event loop.

        Unlike the validation test above, delivery is NOT mocked away: the
        whole point is that ``deliver_pinned_webhook`` itself reaches
        ``SafeUrl.parse(pin=True)``. Only the network POST is stubbed, so a
        parse left inline resolves on the loop thread and this fails.
        """
        from app.api.webhooks import deliver_pinned_webhook

        block_private_targets(monkeypatch)
        resolver_threads = _spy_resolver_threads(monkeypatch)

        # Stage only the HTTP answer: SafeUrl.parse + connect() must still run,
        # and connect() is pure (no I/O), so no network call is attempted. The
        # client is a real httpx.AsyncClient over a scripted transport, so the
        # delivery POST, the streaming read and the close are all real.
        delivered = record_webhook_delivery(monkeypatch)

        await deliver_pinned_webhook(
            "http://public.example.com/cb",
            b"{}",
            host_policy=HostPolicy.unrestricted(),
        )

        assert delivered, "delivery never ran; the test is not exercising the delivery path"
        assert resolver_threads, "socket.getaddrinfo was never called during delivery"
        loop_thread = threading.current_thread()
        for thread in resolver_threads:
            assert thread is not loop_thread, (
                f"delivery resolved DNS on the event-loop thread {thread.name!r}; "
                "it must run on a dns-resolve worker thread"
            )
            assert thread.name.startswith("dns-resolve"), (
                f"delivery did not reuse the dns-resolve pool (ran on {thread.name!r})"
            )

    async def test_validate_webhook_url_async_rejects_private(self, monkeypatch):
        from app.api.webhooks import validate_webhook_url_async

        with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError):
            await validate_webhook_url_async(
                "http://evil.example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )
