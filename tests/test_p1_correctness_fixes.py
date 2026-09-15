"""TDD regressions for six pre-validated correctness defects (P1).

Each test reproduces one reported bug as a behavior assertion and was written
BEFORE its fix. Tests deliberately avoid implementation details: they observe
public behavior (webhook delivery, scheduler result streaming, URL resolution,
diagnostics redaction, cache byte accounting, DNS offloading).
"""

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _testkit import block_private_targets

from app.api.async_crawl_runner import run_async_crawl
from app.crawl.result_store import CrawlResultStore
from app.models import CrawlTask


class TestC3AsyncCrawlFailureWebhook:
    """C3: the async-crawl FAILURE webhook must actually be delivered.

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
        delivered_url, payload = post_webhook.await_args.args
        assert delivered_url == webhook_url, delivered_url
        # Payload must be a real UnifiedCrawlResponse shape, not a coroutine.
        assert isinstance(payload, dict), f"payload must be a dict, got {type(payload)}"
        assert payload["request_id"] == request_id, payload
        assert payload["success"] is False, payload
        assert isinstance(payload["results"], list), payload


class TestH1CircuitBreakerOnResult:
    """H1: with ``on_result`` set, an open circuit must spill the failed
    envelope through ``on_result`` instead of writing ``results[index]``
    (which is ``None`` in that mode -> TypeError, swallowed by
    ``gather(..., return_exceptions=True)`` -> the URL silently vanishes).
    """

    async def test_open_circuit_spills_via_on_result(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(
            global_limit=2,
            per_domain_limit=1,
            delay_seconds=0.0,
            circuit_threshold=3,
            circuit_cooldown_base=60.0,
        )
        # Trip the breaker by hitting the consecutive-failure threshold.
        for _ in range(3):
            scheduler.record_failure("example.com")
        assert scheduler.is_circuit_open("example.com")

        spilled: list[tuple[int, object]] = []

        async def on_result(index, result):
            spilled.append((index, result))

        async def worker(job):  # pragma: no cover - must never run
            raise AssertionError("worker must not run while the circuit is open")

        job = CrawlTask(
            index=0,
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            domain="example.com",
        )
        # The bug raises TypeError inside gather with return_exceptions=True,
        # so run() returns normally but delivers nothing.
        returned = await scheduler.run([job], worker, on_result=on_result)

        assert returned is None, "on_result mode must return None"
        assert len(spilled) == 1, f"expected 1 spilled result, got {len(spilled)}"
        index, result = spilled[0]
        assert index == 0
        assert result.status == "failed", result
        assert result.classification == "network_error", result
        assert result.error == "circuit_breaker_open", result
        assert result.diagnostics.get("circuit_breaker") is True, result.diagnostics


class TestH2WholeSiteRedirectedUrlResolution:
    """H2: whole-site results are keyed by final_url but looked up by the
    pre-redirect frontier URL, so a redirected page misses -> "transient" ->
    re-queued forever, then counted failed and dropped: its content never
    reaches job.results.
    """

    async def test_frontier_url_resolves_via_final_url(self, job_store, monkeypatch):
        frontier_url = "http://example.com/page"
        final_url = "https://example.com/page/"  # http->https + trailing slash

        job = job_store.create(frontier_url, {"limits": {"max_depth": 1}})
        job_id = job.job_id

        call_count = {"n": 0}

        async def fake_execute_crawl(payload, *, settings=None, shutdown_event=None, **kwargs):
            call_count["n"] += 1
            # The engine reports the item keyed by its own URLs; the frontier
            # URL is the ORIGINAL (pre-redirect) one.
            return {
                "results": [
                    {
                        "original_url": frontier_url,
                        "normalized_url": frontier_url,
                        "final_url": final_url,
                        "status": "success",
                        "classification": "rendered",
                        "selected_strategy": "HTTP_DECLARED_CRAWLER",
                        "text": "page content",
                        "markdown": "page content",
                        "html": "<html><body>page content</body></html>",
                        "extraction_method": "http_html",
                        "quality_score": 1.0,
                    }
                ]
            }

        monkeypatch.setattr("app.api.job_runner._execute_crawl", fake_execute_crawl)

        from app.api.job_runner import _run_job

        await _run_job(job_id, job_store, MagicMock(), asyncio.Event())

        finished = job_store.load(job_id)
        assert finished is not None
        # The content must land in job.results exactly once (success), not be
        # re-queued until retries exhaust and then dropped.
        assert len(finished.results) == 1, f"results={finished.results}"
        assert finished.results[0]["status"] == "success", finished.results[0]
        assert finished.stats.get("total_failed", 0) == 0, finished.stats
        # And the frontier URL must be committed visited (not re-crawled).
        assert frontier_url in finished.visited, finished.visited


class TestH4FailureDiagnosticsRedaction:
    """H4: the FAILURE path dumped the raw ``request_profile`` (which can carry
    live cookies, fingerprint_seed, cookie_namespace) into diagnostics, while
    the SUCCESS path exports only ``user_agent_family`` + ``cookie_jar``.
    Diagnostics reach the API response, result store, persisted job.results and
    webhook payloads, so the failure path must emit the same redacted shape.
    """

    async def test_failure_diagnostics_redact_request_profile(self, monkeypatch):
        from _testkit import http_strategy_policy, invoke_http_strategy

        policy = http_strategy_policy()
        # Seed the profile with exactly the sensitive fields the leak exposes.
        policy["request_profile"] = {
            "user_agent_family": "chrome",
            "cookie_jar": "session-jar",
            "cookies": {"session": "SECRET-TOKEN", "csrf": "abc"},
            "fingerprint_seed": 12345,
            "cookie_namespace": "tenant-secret",
        }

        def raising_resp(url):
            raise ConnectionError("connection refused")

        result = await invoke_http_strategy(monkeypatch, policy=policy, resp=raising_resp)

        assert result.status == "failed", result.status
        profile = result.diagnostics["request_profile"]

        # Only the redacted keys the success path emits may appear.
        assert set(profile.keys()) == {"user_agent_family", "cookie_jar"}, profile
        assert profile["user_agent_family"] == "chrome", profile
        assert profile["cookie_jar"] == "session-jar", profile
        # Raw secrets must not survive anywhere in diagnostics.
        for secret in ("SECRET-TOKEN", "fingerprint_seed", "cookie_namespace", "cookies"):
            assert secret not in str(result.diagnostics), str(result.diagnostics)


class TestM1ContentCacheExpiryAccounting:
    """M1: reading an expired (or unreadable) entry popped ``_index`` but left
    ``_index_sizes``/``_index_total_bytes`` untouched. The phantom bytes keep
    ``_over_limits()`` true, so the next ``set()``'s ``_enforce_limits`` evicts
    LIVE LRU entries it should not -- a sustained self-inflicted cache wipe-out
    on a TTL-busy cache.
    """

    def test_expired_read_frees_byte_accounting(self, tmp_path):
        from app.crawl.content_cache import ContentCache

        now = [1000.0]
        cache = ContentCache(
            tmp_path / "cache",
            default_ttl=10,
            clock=lambda: now[0],
            max_entries=100,
            # Tight budget: two entries fit, three do not.
            max_total_bytes=10_000,
        )

        cache.set("a", "x" * 100)
        cache.set("b", "x" * 100)
        size_a = cache._index_sizes["a"]
        total_before = cache._index_total_bytes
        assert total_before > 0

        # Advance past the TTL and read the now-expired entry.
        now[0] = 2000.0
        assert cache.get("a") is None, "expired entry must read as None"

        # The expired read must free A's byte accounting (via _drop_locked),
        # not just the index entry.
        assert cache._index_total_bytes == total_before - size_a, (
            f"phantom bytes: total {cache._index_total_bytes} != "
            f"{total_before - size_a} (expired entry's {size_a} bytes still counted)"
        )

    def test_set_after_expired_read_does_not_evict_live_entry(self, tmp_path):
        from app.crawl.content_cache import ContentCache

        now = [1000.0]
        cache = ContentCache(
            tmp_path / "cache",
            default_ttl=10,
            clock=lambda: now[0],
            max_entries=100,
            max_total_bytes=10_000,
        )
        cache.set("a", "x" * 100)
        cache.set("b", "x" * 100)
        # Budget for exactly two entries.
        unit = cache._index_sizes["b"]
        cache.max_total_bytes = 2 * unit + unit // 2

        now[0] = 2000.0
        assert cache.get("a") is None  # expired read frees A's bytes (or not)
        now[0] = 1000.0  # keep B and C unexpired for the assertions

        cache.set("c", "x" * 100)
        # B is live and must not be sacrificed to phantom A bytes. Clear the
        # RAM mirror so the assertion reads the disk-backed index, not RAM.
        cache._memory_cache.clear()
        assert cache.get("b") == "x" * 100, "live LRU entry evicted by phantom bytes"


class TestM6WebhookValidationOffLoop:
    """M6: ``validate_webhook_url`` calls ``SafeUrl.parse(pin=True)`` ->
    synchronous ``socket.getaddrinfo`` on the event loop. A caller-chosen
    webhook hostname with a slow/blackholed resolver therefore stalls the
    whole FastAPI loop. Crawl targets already offload the identical resolution
    via ``_build_pinned_request_async`` (a dedicated dns-resolve thread pool);
    webhook validation must reuse that off-thread path, not a second one.
    """

    async def test_post_webhook_validates_off_the_event_loop(self, monkeypatch):
        import threading

        from app.api.webhooks import post_webhook

        block_private_targets(monkeypatch)
        resolver_threads: list[threading.Thread] = []

        def spy_getaddrinfo(host, *args, **kwargs):
            resolver_threads.append(threading.current_thread())
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

        monkeypatch.setattr(socket, "getaddrinfo", spy_getaddrinfo)
        # Neutralize delivery so the ONLY resolution is the validation parse.
        monkeypatch.setattr("app.api.webhooks.deliver_pinned_webhook", AsyncMock())

        await post_webhook("http://public.example.com/cb", {"results": []})

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

    async def test_validate_webhook_url_async_rejects_private(self, monkeypatch):
        from app.api.webhooks import validate_webhook_url_async

        block_private_targets(monkeypatch)
        with patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        ):
            with pytest.raises(ValueError):
                await validate_webhook_url_async("http://evil.example.com/callback")
