"""HTTP strategy edge cases: fallback chains, streaming caps, timeout handling."""

import multiprocessing
import os

from _testkit import *
from helpers.server import _Handler as _BaseHandler
from helpers.server import spawn_threaded_server

from app.validation import HostPolicy


class _SimpleHandler(_BaseHandler):
    """Minimal 200/text-html handler for the subprocess cffi smoke test."""

    def do_GET(self):
        body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _cffi_smoke_worker(url: str, out_queue: multiprocessing.Queue) -> None:
    """Subprocess body for the cffi smoke test (must be module-level to pickle)."""
    import asyncio

    os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"

    from app.crawl.strategies.http import _default_cffi_fetcher
    from app.validation import HostPolicy

    try:
        result = asyncio.run(
            _default_cffi_fetcher(
                url,
                {},
                10,
                50 * 1024 * 1024,
                policy=HostPolicy.unrestricted(),
            )
        )
        out_queue.put(("ok", result.status_code))
    except Exception as error:
        out_queue.put(("err", type(error).__name__, str(error)[:160]))


class TestHttpStrategyEdges:
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
        """real_cffi subprocess: a normal fetch returns the served 200.

        In-process real cffi is banned (#1744/#1746): under fast_sleeps the
        curl watchdog busy-spins the GIL and wedges the pytest process, so
        the fetch runs in an isolated interpreter with a hard join.
        """
        import multiprocessing

        server = spawn_threaded_server(_SimpleHandler)
        url = f"http://127.0.0.1:{server.server_port}/"
        out_queue: multiprocessing.Queue = multiprocessing.Queue()
        worker = multiprocessing.Process(target=_cffi_smoke_worker, args=(url, out_queue))
        try:
            worker.start()
            worker.join(timeout=10.0)
            if worker.is_alive():
                worker.terminate()
                worker.join()
                pytest.fail("cffi fetcher hung past 10s on a normal response")
        finally:
            server.shutdown()
            server.server_close()

        assert worker.exitcode == 0, f"worker crashed: exitcode={worker.exitcode}"
        assert not out_queue.empty(), "worker produced no result"
        outcome, status_or_exc = out_queue.get()
        assert outcome == "ok" and status_or_exc == 200

    async def test_http_strategy_result_httpx_request_error(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        cache = HttpCache()

        def failing_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            import httpx

            raise httpx.RequestError("connection failed", request=None)

        result = await _http_strategy_result(
            original_url="http://fail.example.com/",
            normalized_url="http://fail.example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=failing_fetcher,
            cache=cache,
            host_policy=HostPolicy.unrestricted(),
        )
        # A bare httpx.RequestError is the generic httpx-level tail of the
        # classifier (more specific branches map to connection_error/tls_error
        # etc.), so it lands on http_error. The old sink hardcoded
        # network_error and masked this; the shared primitive surfaces the
        # real code now.
        assert result.classification == "http_error"

    async def test_http_strategy_result_httpx_http_status_error(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        cache = HttpCache()

        def error_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
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
            policy={
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=error_fetcher,
            cache=cache,
            host_policy=HostPolicy.unrestricted(),
        )
        assert result.classification == "http_status_error"

    def test_decode_body_unicode_decode_error(self):
        from app.crawl.strategies.http import _decode_body

        body = b"\xff\xfe"
        result = _decode_body(body, "text/html")
        assert isinstance(result, str)

    async def test_http_strategy_result_binary_classification(self):
        from app.constants import EXTRACTION_METHOD_BINARY_NO_TEXT
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        cache = HttpCache()

        def binary_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
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
            policy={
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=binary_fetcher,
            cache=cache,
            host_policy=HostPolicy.unrestricted(),
        )
        assert result.classification == EXTRACTION_METHOD_BINARY_NO_TEXT
