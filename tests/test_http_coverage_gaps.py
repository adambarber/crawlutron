"""TestHttpCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestHttpCoverageGaps:
    """Cover remaining branches in http.py"""

    def test_charset_from_bytes_import_error(self):
        from app.crawl.strategies.http import _decode_body
        body = b"test content"
        result = _decode_body(body, "text/plain; charset=utf-8")
        assert result == "test content"

    async def test_close_async_http_client_error(self):
        from app.crawl.strategies.http import close_async_http_client
        await close_async_http_client()
        await close_async_http_client()

    def test_is_private_or_loopback_ip_invalid(self):
        from app.crawl.strategies.http import _is_private_or_loopback_ip
        assert _is_private_or_loopback_ip("127.0.0.1")
        assert _is_private_or_loopback_ip("192.168.1.1")
        assert _is_private_or_loopback_ip("10.0.0.1")
        assert _is_private_or_loopback_ip("::1")
        assert not (_is_private_or_loopback_ip("8.8.8.8"))
        assert _is_private_or_loopback_ip("invalid")

    def test_default_cffi_fetcher_response(self):
        from app.crawl.strategies.http import _default_cffi_fetcher
        result = _spawn_fetch(lambda s: asyncio.run(_default_cffi_fetcher(s.base_url + "/", {}, 10, 50*1024*1024)))
        assert result.status_code == 200

    async def test_http_strategy_result_httpx_request_error(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        cache = HttpCache()
        def failing_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            import httpx
            raise httpx.RequestError("connection failed", request=None)
        result = await _http_strategy_result(
            original_url="http://fail.example.com/",
            normalized_url="http://fail.example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=failing_fetcher,
            cache=cache,
        )
        assert result.classification == "network_error"

    async def test_http_strategy_result_httpx_http_status_error(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        cache = HttpCache()
        def error_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            import httpx
            class MockResponse:
                content = b"error"
                status_code = 500
                headers = {"content-type": "text/plain"}
                url = "http://error.example.com/"
            raise httpx.HTTPStatusError("server error", request=None, response=MockResponse())
        result = await _http_strategy_result(
            original_url="http://error.example.com/",
            normalized_url="http://error.example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=error_fetcher,
            cache=cache,
        )
        assert result.classification == "network_error"

    def test_decode_body_unicode_decode_error(self):
        from app.crawl.strategies.http import _decode_body
        body = b"\xff\xfe"
        result = _decode_body(body, "text/html")
        assert isinstance(result, str)

    async def test_http_strategy_result_binary_classification(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        from app.constants import EXTRACTION_METHOD_BINARY_NO_TEXT
        cache = HttpCache()
        def binary_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "application/octet-stream"}
                body = b"binary"
                final_url = url
                elapsed_ms = 10
            return Resp()
        result = await _http_strategy_result(
            original_url="http://binary.example.com/file.bin",
            normalized_url="http://binary.example.com/file.bin",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=binary_fetcher,
            cache=cache,
        )
        assert result.classification == EXTRACTION_METHOD_BINARY_NO_TEXT


