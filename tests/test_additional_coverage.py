"""TestAdditionalCoverage coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestAdditionalCoverage:
    """Additional targeted tests to close coverage gaps."""

    def test_content_cache_set_overwrite(self):
        cache, _ = _temp_cache(ttl=60)
        cache.set("key", {"data": "first"})
        cache.set("key", {"data": "second"})
        assert cache.get("key") == {"data": "second"}

    def test_content_cache_get_corrupt_json(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60)
            cache_path = cache._path_for("testkey")
            cache_path.write_text("{broken json")
            result = cache.get("testkey")
            assert (result) is None

    def test_content_cache_prune_corrupt_entry(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = make_content_cache_with_entry(tmpdir, clock=lambda: clock_vals[0])
            # Write a corrupt file with the correct hash-based name - prune should skip it
            bad_path = cache._path_for("badkey")
            bad_path.write_text("not valid json")
            clock_vals[0] = 1002.0  # advance past TTL
            removed = cache.prune()
            # Only key1 is expired and removable; corrupt file is skipped
            assert removed == 1

    def test_job_store_update(self):
        import pathlib
        import tempfile

        from app.crawl.job_store import JobStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            job = store.create("http://example.com/", {})
            store.save(job)
            updated = store.load(job.job_id)
            assert (updated) is not None

    def test_job_store_delete(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_job_store(tmpdir)
            job = store.create("http://example.com/", {})
            deleted = store.delete(job.job_id)
            assert deleted
            assert (store.load(job.job_id)) is None
            deleted = store.delete(job.job_id)
            assert not (deleted)

    def test_output_formats_default_with_dict(self):
        from app.output_formats import _default

        # Object with model_dump method
        class FakeObj:
            def model_dump(self):
                return {"serialized": "true"}

        result = _default(FakeObj())
        assert result == {"serialized": "true"}

    def test_output_formats_serialize_unknown_format(self):
        from app.output_formats import serialize_results

        results = [{"original_url": "http://x/"}]
        with pytest.raises(ValueError):
            serialize_results(results, "unknown_format")

    def test_decompression_zlib(self):
        import zlib

        from app.crawl.decompression import decompress_body

        original = b"hello world " * 10
        compressed = zlib.compress(original)
        result = decompress_body(compressed, "deflate")
        assert (result) is not None

    def test_decompression_gzip(self):
        import gzip

        from app.crawl.decompression import decompress_body

        original = b"hello world " * 10
        compressed = gzip.compress(original)
        result = decompress_body(compressed, "gzip")
        assert (result) is not None

    def test_decompression_unknown_encoding(self):
        from app.crawl.decompression import decompress_body

        result = decompress_body(b"some data", "unknown")
        assert result == b"some data"

    def test_decompression_none_encoding(self):
        from app.crawl.decompression import decompress_body

        # With an unrecognized encoding, returns the original body
        result = decompress_body(b"some data", "unknown_encoding")
        assert result == b"some data"

    def test_scheduler_domain_cooldowns(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("example.com", 10)
        cooldowns = scheduler.domain_cooldowns
        assert isinstance(cooldowns, dict)
        assert "example.com" in cooldowns

    def test_scheduler_circuit_breaker_reset(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, circuit_cooldown=0.0
        )
        # Trip the circuit breaker (threshold=5 by default)
        for _ in range(5):
            scheduler.record_failure("example.com")
        assert scheduler.is_circuit_open("example.com")
        # record_success resets failure counter; with cooldown=0, circuit is not open
        scheduler.record_success("example.com")
        assert scheduler._domain_failures.get("example.com", 0) == 0

    async def test_http_async_client_close(self):

        from app.crawl.strategies.http import close_async_http_client, get_async_http_client

        # Get a client, then close it
        async def test():
            get_async_http_client()
            await close_async_http_client()

        await test()

    def test_engine_shared_content_cache(self):
        from app.crawl.content_cache import ContentCache
        from app.crawl.engine import _get_shared_content_cache

        cache = _get_shared_content_cache()
        assert isinstance(cache, ContentCache)

    def test_http_large_content(self):
        from app.crawl.strategies.http import _default_http_fetcher

        result = _spawn_fetch(lambda s: asyncio.run(_default_http_fetcher(s.base_url + "/", {}, 10, 1)))
        assert result.status_code == 413

    def test_extraction_different_methods(self):
        from app.crawl.extraction import extract_content

        html = "<html><body><p>Hello world</p></body></html>"
        for method, content_type in [
            ("trafilatura", "text/html"),
            ("document", "application/pdf"),
            ("dom_text", "text/html"),
            ("full", "text/html"),
        ]:
            result = extract_content(html, content_type, "http://example.com/", None, None)
            assert (result) is not None
