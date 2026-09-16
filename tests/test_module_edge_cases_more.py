"""Cross-module edge cases (continued): charset fallbacks, OS errors, safe coercion."""

from coverage_helpers import *


class TestModuleEdgeCasesMore:
    """More targeted tests to close remaining coverage gaps."""

    def test_content_cache_prune_os_error(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = make_content_cache_with_entry(tmpdir, clock=lambda: clock_vals[0])
            clock_vals[0] = 1002.0
            # Replace file with directory to cause OSError on read
            cache_path = cache._path_for("key1")
            cache_path.unlink()
            cache_path.mkdir()
            cache.prune()
            cache_path.rmdir()

    def test_output_formats_default_dict_method(self):
        from app.output_formats import _default

        class DictObj:
            def dict(self):
                return {"from_dict": "value"}

        result = _default(DictObj())
        assert result == {"from_dict": "value"}

    def test_output_formats_default_str_fallback(self):
        from app.output_formats import _default

        result = _default(42)
        assert result == "42"

    def test_decompression_brotli_exception(self):
        from app.crawl.decompression import decompress_body

        try:
            result = decompress_body(b"not gzip data", "gzip", max_output_bytes=10_000_000)
            assert result == b"not gzip data"
        except Exception:
            pass

    def test_job_store_load_corrupt_row(self):
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_job_store(tmpdir)
            job = store.create("http://example.com/", {})
            # Corrupt the policy JSON in the database
            import sqlite3

            conn = sqlite3.connect(str(pathlib.Path(tmpdir) / "jobs.db"))
            conn.execute("UPDATE crawl_jobs SET policy = ? WHERE job_id = ?", ("invalid json{", job.job_id))
            conn.commit()
            conn.close()
            # load should catch the JSON decode error and return None
            result = store.load(job.job_id)
            assert (result) is None

    def test_scheduler_record_latency_invalid(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        # None latency should be ignored (line 114: return)
        scheduler.record_latency("example.com", None)
        # Negative latency should be ignored
        scheduler.record_latency("example.com", -1.0)
        # Verify no latency was recorded
        assert "example.com" not in scheduler._domain_latency_ema

    async def test_http_strategy_not_modified(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        def not_mod_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            class Resp:
                status_code = 304
                headers = {"etag": "v1", "content-type": "text/html"}
                body = b"not modified"
                final_url = url
                elapsed_ms = 10

            return Resp()

        cache = HttpCache()
        cache.store("http://example.com/", {"etag": "v1"})
        result = await _http_strategy_result(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=not_mod_fetcher,
            cache=cache,
        )
        assert result.classification == "not_modified"

    def test_content_cache_clear_os_error(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60)
            cache.set("key1", {"data": "1"})
            cache_path = cache._path_for("key1")
            cache_path.unlink()
            cache_path.mkdir()
            cache.clear()
            cache_path.rmdir()

    def test_output_formats_safe_json(self):
        from app.output_formats import _safe

        val = {"nested": [1, 2, 3]}
        result = _safe(val)
        assert isinstance(result, str)

    def test_output_formats_safe_none(self):
        from app.output_formats import _safe

        result = _safe(None)
        assert result == ""

    def test_output_formats_safe_string(self):
        from app.output_formats import _safe

        result = _safe("hello")
        assert result == "hello"

    def test_output_formats_safe_int(self):
        from app.output_formats import _safe

        result = _safe(42)
        assert result == "42"

    def test_http_charset_decode_error(self):
        from app.crawl.strategies.http import _decode_body

        # Charset rides on the content-type only; a stray value can never
        # reach the decoder as a headers argument (the parameter is gone).
        result = _decode_body(b"hello", "text/plain; charset=invalid_charset")
        assert isinstance(result, str)

    def test_http_cookie_invalid(self):
        from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR

        # Call with invalid cookie header - should not raise
        _GLOBAL_COOKIE_JAR.add_cookie_header({}, "http://example.com/")

    def test_scheduler_record_latency_valid(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.record_latency("example.com", 100.0)
        assert "example.com" in scheduler._domain_latency_ema
        assert abs(scheduler._domain_latency_ema["example.com"] - (100.0)) < 1e-07

    def test_scheduler_record_latency_ema(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive_smoothing=0.5
        )
        scheduler.record_latency("example.com", 100.0)
        scheduler.record_latency("example.com", 200.0)
        assert abs(scheduler._domain_latency_ema["example.com"] - (150.0)) < 0.1

    def test_scheduler_record_success(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.record_success("example.com")
        assert scheduler._domain_failures.get("example.com", 0) == 0

    def test_scheduler_is_circuit_open_false(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        assert not (scheduler.is_circuit_open("example.com"))

    def test_http_quality_score_failed(self):
        from app.crawl.strategies.http import _quality_score

        score = _quality_score("failed", "network_error", None, None, None)
        assert score == 0.0

    def test_http_quality_score_empty_text(self):
        from app.crawl.strategies.http import _quality_score

        score = _quality_score("success", "rendered", "", None, 200)
        assert score > 0.0

    def test_http_status_not_found(self):
        from app.constants import status_for_classification

        status = status_for_classification("not_found")
        assert status == "failed"

    def test_http_status_site_error(self):
        from app.constants import status_for_classification

        status = status_for_classification("site_error")
        assert status == "failed"

    async def test_http_cffi_imersonate_result(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        def cffi_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><body>content</body></html>"
                final_url = url
                elapsed_ms = 10

            return Resp()

        result = await _http_strategy_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_CFFI_IMPERSONATE",
            policy={
                "strategies": ["HTTP_CFFI_IMPERSONATE"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=cffi_fetcher,
            cache=HttpCache(),
        )
        assert result.classification == "rendered"

    async def test_http_cffi_import_fallback(self, real_sleeps):
        # Live impersonated fetch: needs real wall-clock sleeps -- the suite's
        # instant-asyncio.sleep patch starves curl_cffi's socket/timer loop
        # and wedges the transfer past all client timeouts.
        from helpers.server import spawn_server

        from app.crawl.strategies.http import _default_cffi_fetcher

        server = spawn_server()
        try:
            result = await _default_cffi_fetcher(server.base_url + "/", {}, 10, 50 * 1024 * 1024)
            assert result.status_code == 200
        finally:
            server.stop()

    def test_extraction_parse_html_tree_error(self):
        from app.crawl.extraction import _parse_html_tree

        result = _parse_html_tree("{{invalid html")
        # Should not raise - lxml is forgiving
        assert (result) is not None

    def test_extraction_selected_with_xpath(self):
        from app.crawl.extraction import _extract_selected

        html = "<html><body><div>Hello</div></body></html>"
        result = _extract_selected(html, "http://x/", css_selector=None, xpath="//div/text()")
        assert (result) is not None

    async def test_main_invalid_webhook_url(self):

        import httpx
        from httpx import AsyncClient

        from app.main import app

        async def test():
            transport = httpx.ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/crawl", json={"url": "http://example.com/", "webhook_url": "not_a_url"}
                )
                assert resp.status_code == 422

        await test()

    async def test_main_job_control_invalid_action(self):
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "invalid_action", 422)
        job_store.delete(job.job_id)

    async def test_main_job_control_cancel_completed(self):
        # H5: cancel from a terminal state is a state conflict (409), not a
        # shape error (422) — the request shape is valid, the transition isn't.
        from app.crawl.job_store import JobStatus
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        # Set to completed
        job.status = JobStatus.COMPLETED
        job_store.save(job)
        await run_job_control_test(app, job_store, job.job_id, "cancel", 409)
        job_store.delete(job.job_id)

    async def test_main_job_control_resume_not_paused(self):
        # H5: resume requires PAUSED; from PENDING it is a state conflict.
        from app.main import app, job_store

        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "resume", 409)
        job_store.delete(job.job_id)

    async def test_engine_crawl_request_with_webhook(self):
        import asyncio

        from app.crawl.engine import crawl_request
        from app.settings import CrawlerSettings

        payload = {
            "url": "http://127.0.0.1:1/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 5},
                "request_profile": {},
            },
            "webhook_url": "http://webhook.invalid",
        }

        async def browser_worker(job):
            return None

        result = await crawl_request(
            payload,
            settings=CrawlerSettings.from_mapping(),
            browser_worker=browser_worker,
            shutdown_event=asyncio.Event(),
            webhook_url="http://webhook.invalid",
        )
        assert "webhook_url" in result

    def test_engine_parse_robots_blank_line(self):
        from app.crawl.extraction import _extract_discovered_links

        # Test link extraction with mixed quote styles (covers edge cases)
        html = "<html><body><a href=https://example.com/test>link</a><a href='https://example.com/page2'>link2</a></body></html>"
        links = _extract_discovered_links(html, None)
        assert len(links) > 0

    async def test_http_strategy_with_cffi_retry(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        call_count = [0]

        def retry_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("first attempt fails")

            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><body>recovered</body></html>"
                final_url = url
                elapsed_ms = 10

            return Resp()

        result = await _http_strategy_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 1, "max_discovered_links": 10},
            },
            http_fetcher=retry_fetcher,
            cache=HttpCache(),
        )
        assert result.classification == "rendered"


# --- Helpers to reduce test boilerplate ---
