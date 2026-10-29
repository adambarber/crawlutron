"""Webhook validation and delivery behavior.

Covers failure-webhook delivery and the requirement that validation and pinned
delivery resolve DNS off the event loop."""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import pathlib
import socket
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import Request

from crawlutron.api.async_crawl_runner import run_async_crawl
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.safety.validation import HostPolicy
from crawlutron.storage.job_store import JobStatus, JobStore
from crawlutron.storage.result_store import CrawlResultStore
from tests._api_harnesses import run_with_webhook as _run_with_webhook
from tests._assertions import block_private_targets, host_resolves_to
from tests._webhook_doubles import record_webhook_delivery


def _webhook_target() -> str:
    """The loopback-safe host the delivery tests agree on."""
    return "http://webhook.example.com/cb"


async def _run_webhook_crawl(job_store, result_store, request_id) -> None:
    """Drive one single-shot webhook crawl to completion (shared pace)."""
    await run_async_crawl(
        request_id,
        CrawlRequest.model_validate({"url": "http://example.com/"}),
        _webhook_target(),
        MagicMock(),
        job_store,
        result_store,
        asyncio.Event(),
    )


async def _post_webhook(monkeypatch, *, status_code: int = 200) -> list[Request]:
    """POST the canonical webhook payload and return the delivered requests."""
    from crawlutron.api import webhooks

    delivered = record_webhook_delivery(monkeypatch, status_code=status_code)
    await webhooks.post_webhook(
        _webhook_target(),
        {"results": []},
        host_policy=HostPolicy.unrestricted(),
    )
    return delivered


def _spy_resolver_threads(monkeypatch: pytest.MonkeyPatch) -> list[threading.Thread]:
    """Record the thread every ``socket.getaddrinfo`` call runs on.

    Both offload tests stage the same spy (public-IP answer plus thread capture)
    and assert against the recorded threads; only what they drive differs."""
    resolver_threads: list[threading.Thread] = []

    def spy_getaddrinfo(host, *args, **kwargs):
        resolver_threads.append(threading.current_thread())
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", spy_getaddrinfo)
    return resolver_threads


class TestAsyncCrawlFailureWebhookDelivery:
    """The async-crawl FAILURE webhook must actually be delivered.

    ``await coro().model_dump()`` parses as ``await (coro().model_dump())``: the
    dump runs on the coroutine (TypeError) and ``except Exception`` swallows it."""

    async def test_failed_async_crawl_delivers_failure_webhook(self, job_store, tmp_path):
        request_id = "crawl_c3_failure"
        job_store.create_single_shot(request_id, "http://example.com/", {"url": "http://example.com/"})

        webhook_url = "http://webhook.example.com/cb"
        result_store = CrawlResultStore(tmp_path / "results.db")

        async def _boom(*args, **kwargs):
            raise RuntimeError("crawl blew up")

        post_webhook = await _run_with_webhook(request_id, _boom, webhook_url, job_store, result_store)

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


class TestAsyncCrawlTerminalCasControlsWhatIsPublished:
    """What the runner publishes is decided by whether its terminal write won.

    All status moves CAS from ``RUNNING``, including the runner's own terminal
    write; discarding that result published a paused crawl and stranded one."""

    REQUEST_ID = "crawl_terminal_cas"

    async def _displaced_crawl(
        self, job_store: JobStore, tmp_path: pathlib.Path, displaced_to: JobStatus
    ) -> tuple[CrawlResultStore, AsyncMock]:
        """Run a crawl whose job is moved off ``RUNNING`` while it runs.

        The displacement runs *inside* ``execute_crawl``: the window the runner
        must survive is between claiming ``RUNNING`` and the terminal write.
        """
        request_id = self.REQUEST_ID
        job_store.create_single_shot(request_id, "http://example.com/", {"url": "http://example.com/"})
        result_store = CrawlResultStore(tmp_path / "results.db")

        async def execute(*args, **kwargs):
            await job_store.aset_status(request_id, displaced_to, expected=JobStatus.RUNNING)
            return {
                "results": [],
                "request_id": request_id,
                "submitted_count": 0,
                "policy_schema_version": "",
            }

        post_webhook = await _run_with_webhook(
            request_id,
            execute,
            "http://webhook.example.com/cb",
            job_store,
            result_store,
        )
        return result_store, post_webhook

    async def test_a_paused_crawl_publishes_nothing_and_fires_no_webhook(self, job_store, tmp_path):
        result_store, post_webhook = await self._displaced_crawl(job_store, tmp_path, JobStatus.PAUSED)
        assert result_store.get(self.REQUEST_ID) is None, (
            "a paused crawl published results it will never serve"
        )
        assert post_webhook.await_count == 0, (
            f"post_webhook awaited {post_webhook.await_count} times; a paused "
            "crawl announced a completion that did not happen"
        )

    async def test_a_crawl_rewound_to_pending_is_published_so_the_client_is_not_stranded(
        self, job_store, tmp_path
    ):
        result_store, post_webhook = await self._displaced_crawl(job_store, tmp_path, JobStatus.PENDING)
        # `recover_orphaned_running_jobs` rewinds RUNNING to PENDING on startup on the assumption that nothing
        # is in flight.
        assert result_store.get(self.REQUEST_ID) is not None, (
            "the terminal write lost to PENDING and the results were dropped, so this job can never complete"
        )
        assert job_store.load(self.REQUEST_ID).status == JobStatus.COMPLETED
        assert post_webhook.await_count == 1, post_webhook.await_count


class TestWebhookValidationRunsOffLoop:
    """Webhook host pinning must run off the event loop; a blackholed resolver
    would otherwise stall the FastAPI loop. Pin as crawl targets do; delivery
    re-pins through the same builder, closing the validation->delivery TOCTOU."""

    async def test_post_webhook_resolves_off_the_event_loop(self, monkeypatch):
        """Every DNS work ``post_webhook`` does (the per-attempt delivery pin, #450)
        must leave the event loop; a blackholed resolver would otherwise stall the
        FastAPI loop. Pin as crawl targets do; delivery re-pins each attempt."""
        from crawlutron.api.webhooks import post_webhook

        block_private_targets(monkeypatch)
        resolver_threads = _spy_resolver_threads(monkeypatch)
        delivered = record_webhook_delivery(monkeypatch)

        await post_webhook(
            "http://public.example.com/cb",
            {"results": []},
            host_policy=HostPolicy.unrestricted(),
        )

        assert delivered, "delivery never ran; the test is not exercising the pin path"
        assert resolver_threads, "socket.getaddrinfo was never called during delivery"
        loop_thread = threading.current_thread()
        for thread in resolver_threads:
            assert thread is not loop_thread, (
                f"the delivery pin resolved DNS on the event-loop thread {thread.name!r}; "
                "it must run on a dns-resolve worker thread"
            )
            assert thread.name.startswith("dns-resolve"), (
                f"the delivery pin did not reuse the dns-resolve pool (ran on {thread.name!r})"
            )

    async def test_deliver_pinned_webhook_parses_off_the_event_loop(self, monkeypatch):
        """The delivery pin must also leave the event loop.

        ``deliver_pinned_webhook`` reaches ``build_pinned_request_async`` itself
        (only the POST is stubbed), so a pin left inline resolves on the loop."""
        from crawlutron.api.webhooks import deliver_pinned_webhook

        block_private_targets(monkeypatch)
        resolver_threads = _spy_resolver_threads(monkeypatch)

        # Stage only the HTTP answer: the pin must still run, and pinning is pure (no I/O), so no network call
        # is attempted.
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
        from crawlutron.api.webhooks import validate_webhook_url_async

        with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError):
            await validate_webhook_url_async(
                "http://evil.example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )


class TestDeliveryTimeValidationFailureStaysBestEffort:
    """A transient validation raise at delivery time must not escape ``post_webhook``
    (#450): the pin path fails closed per attempt, and the caller's outcome stands."""

    async def test_a_resolver_failure_at_delivery_time_does_not_raise_out_of_post_webhook(self, monkeypatch):
        """A DNS blip during delivery is a lost callback, never a raised error: the
        best-effort contract belongs to the whole delivery, pre-loop check included."""
        from crawlutron.api import webhooks

        async def resolver_blip(url, *, host_policy):
            raise OSError("resolver timeout")

        monkeypatch.setattr(webhooks, "validate_webhook_url_async", resolver_blip)
        record_webhook_delivery(monkeypatch)
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            await webhooks.post_webhook(
                _webhook_target(),
                {"results": []},
                host_policy=HostPolicy.unrestricted(),
            )


class TestErrorBranchTerminalCasGatesPublish:
    """The error webhook publishes only when the terminal FAILED write won.

    The error branch used to post unconditionally; a paused/cancelled job still
    got an error callback for a run an operator already stopped."""

    async def test_a_paused_job_that_fails_publishes_no_error_webhook(self, job_store, tmp_path):
        request_id = "crawl_err_paused"
        job_store.create_single_shot(request_id, "http://example.com/", {"url": "http://example.com/"})
        result_store = CrawlResultStore(tmp_path / "results.db")

        # The operator pauses while the crawl is failing; the terminal FAILED
        # write lost the CAS, so no error callback may announce it.
        async def explode_then_pause(*args, **kwargs):
            await job_store.aset_status(request_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)
            raise RuntimeError("crawl blew up")

        with patch("crawlutron.api.async_crawl_runner.execute_crawl", side_effect=explode_then_pause):
            with patch("crawlutron.api.webhooks.post_webhook", new_callable=AsyncMock) as post_webhook:
                await _run_webhook_crawl(job_store, result_store, request_id)

        assert post_webhook.await_count == 0, (
            f"error webhook fired {post_webhook.await_count} times for a PAUSED job; "
            "the terminal FAILED CAS lost and must not publish"
        )


class TestWebhookSignature:
    """Webhook callbacks must be HMAC-signed so a receiver can prove the delivery
    came from this service and was not tampered with in transit."""

    async def test_payload_is_hmac_signed_when_secret_is_set(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_SECRET", "s3cret-webhook-key")
        delivered = await _post_webhook(monkeypatch)

        assert len(delivered) == 1, delivered
        signature = delivered[0].headers.get("X-Crawlutron-Webhook-Signature", "")
        assert signature.startswith("sha256="), (
            f"no HMAC signature header; receiver cannot verify this callback: {signature!r}"
        )
        timestamp = delivered[0].headers.get("X-Crawlutron-Webhook-Timestamp", "")
        assert timestamp, "no timestamp header; the replay window cannot be enforced"
        expected = hmac.new(
            b"s3cret-webhook-key",
            timestamp.encode() + b"." + delivered[0].content,
            hashlib.sha256,
        ).hexdigest()
        assert signature == f"sha256={expected}", (
            "the signature does not bind the timestamp; a captured delivery can be replayed"
        )

    async def test_body_only_signature_does_not_verify(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_SECRET", "s3cret-webhook-key")
        delivered = await _post_webhook(monkeypatch)

        body_only = hmac.new(b"s3cret-webhook-key", delivered[0].content, hashlib.sha256).hexdigest()
        assert delivered[0].headers["X-Crawlutron-Webhook-Signature"] != f"sha256={body_only}", (
            "a body-only signature verifies; the timestamp never entered the digest"
        )

    async def test_no_signature_header_when_secret_unset(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_WEBHOOK_SECRET", raising=False)
        delivered = await _post_webhook(monkeypatch)

        assert not delivered[0].headers.get("X-Crawlutron-Webhook-Signature"), (
            "an unsigned callback claims to be signed; headers must be absent without a secret"
        )


class TestWebhookRetryBackoff:
    """Transient webhook failures must be retried with backoff, not dropped."""

    async def test_transient_5xx_is_retried(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", "0")
        delivered = await _post_webhook(monkeypatch, status_code=500)

        assert len(delivered) >= 2, (
            f"a 500 receiver was tried {len(delivered)} times; a transient webhook "
            "failure is dropped without retry/backoff"
        )

    async def test_permanent_4xx_is_not_retried(self, monkeypatch):
        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", "0")
        delivered = await _post_webhook(monkeypatch, status_code=400)

        assert len(delivered) == 1, (
            f"a 4xx receiver was tried {len(delivered)} times; a permanent refusal must not be retried"
        )

    async def test_3xx_is_not_delivery(self, monkeypatch):
        """A 3xx means the receiver did not answer with a final verdict at the
        callback URL: it is not delivery, so it follows the transient policy and
        is retried rather than silently counted as success."""
        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", "0")
        delivered = await _post_webhook(monkeypatch, status_code=302)

        assert len(delivered) >= 2, (
            f"a 302 receiver was tried {len(delivered)} times; a 3xx is not delivery "
            "and must be retried, not accepted as delivered"
        )

    async def test_retry_sleeps_with_exponential_backoff(self, monkeypatch):
        """The gap between retries grows; a flat immediate re-fire is not a backoff."""
        from crawlutron.api import webhooks

        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_BACKOFF_BASE_MS", "100")
        waited: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr(webhooks.asyncio, "sleep", fake_sleep)
        await _post_webhook(monkeypatch, status_code=500)

        assert len(waited) >= 2, f"no backoff between retries: {waited}"
        assert waited == sorted(waited) and waited[-1] > waited[0], (
            f"retries did not back off exponentially: {waited}"
        )


class TestShutdownDrainsInFlightWebhookDelivery:
    """A cancelled runner must not drop the callback whose terminal write landed;
    shutdown drains in-flight deliveries before the store closes."""

    async def test_shutdown_drain_awaits_an_in_flight_webhook(self, monkeypatch):
        from crawlutron.api import webhooks

        started = asyncio.Event()
        completed = asyncio.Event()
        release = asyncio.Event()

        async def slow_delivery(url, payload, *, host_policy):
            started.set()
            await release.wait()
            completed.set()

        monkeypatch.setattr(webhooks, "validate_webhook_url_async", AsyncMock())
        monkeypatch.setattr(webhooks, "deliver_pinned_webhook", slow_delivery)

        task = asyncio.create_task(
            webhooks.post_webhook_guarded(
                "http://webhook.example.com/cb",
                {"results": []},
                host_policy=HostPolicy.unrestricted(),
            )
        )
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # The terminal write is done; the in-flight webhook must not be dropped.
        release.set()
        await webhooks.drain_webhook_deliveries()

        assert completed.is_set(), "the in-flight webhook callback was dropped at shutdown"


class TestShutdownDrainIsBounded:
    """A hung delivery cannot wedge shutdown: the drain has a total deadline
    and logs what it abandoned (#364)."""

    async def test_a_hanging_delivery_abandons_within_the_deadline(self, monkeypatch, caplog):
        from crawlutron.api import webhooks

        started = asyncio.Event()
        never = asyncio.Event()

        async def hung_delivery(url, payload, *, host_policy):
            started.set()
            await never.wait()

        monkeypatch.setattr(webhooks, "deliver_pinned_webhook", hung_delivery)

        webhooks.start_webhook_delivery(
            "http://webhook.example.com/cb",
            {"results": []},
            host_policy=HostPolicy.unrestricted(),
        )
        await started.wait()

        # The production budget derives from the retry constants (covered by the
        # formula test); boundedness here is proven with a tiny deadline.
        monkeypatch.setattr(webhooks, "webhook_drain_deadline_seconds", lambda: 0.05)
        caplog.set_level(logging.WARNING, logger=webhooks.logger.name)

        deadline = asyncio.get_running_loop().time() + 10
        await webhooks.drain_webhook_deliveries()
        assert asyncio.get_running_loop().time() <= deadline, (
            "the drain waited past its deadline on a hung delivery"
        )
        abandoned = [r for r in caplog.records if r.getMessage() == "webhook_drain_abandoned"]
        assert abandoned, "the drain abandoned a hung delivery without logging it"


def test_drain_deadline_is_the_retry_budget():
    """The drain deadline is one delivery's worst path from the retry budget (#364)."""
    from crawlutron.api import webhooks

    attempts = webhooks.webhook_max_attempts()
    base_s = webhooks.webhook_backoff_base_ms() / 1000.0
    expected = attempts * 30.0 + sum(min(base_s * (2**a), 60.0) for a in range(attempts - 1))

    deadline = webhooks.webhook_drain_deadline_seconds()

    assert deadline == expected
    assert deadline == pytest.approx(90.6)
