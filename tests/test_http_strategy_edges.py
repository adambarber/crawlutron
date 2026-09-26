import multiprocessing
import os

import pytest
from _fetcher_factories import failing_fetcher, request_fetcher, run_http_strategy, scripted_fetcher
from _policy_factories import http_strategy_policy
from helpers.server import _Handler as _BaseHandler
from helpers.server import spawn_threaded_server


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

    from app.crawl.strategies.fetchers import _default_cffi_fetcher
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
        from app.crawl.strategies.body_decode import _decode_body

        body = b"test content"
        result = _decode_body(body, "text/plain; charset=utf-8")
        assert result == "test content"

    async def test_close_async_http_client_error(self):
        from app.crawl.strategies.http_client_pool import close_async_http_client

        await close_async_http_client()
        await close_async_http_client()

    def test_is_private_or_loopback_ip_invalid(self):
        from app.crawl.strategies.pinned_transport import _is_private_or_loopback_ip

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

        cache = HttpCache()

        import httpx

        unreachable_fetcher = failing_fetcher(httpx.RequestError("connection failed", request=None))

        result = await run_http_strategy(
            url="http://fail.example.com/",
            policy=http_strategy_policy(),
            http_fetcher=unreachable_fetcher,
            cache=cache,
        )
        # A bare httpx.RequestError is the generic httpx-level tail of the
        # classifier (more specific branches map to connection_error/tls_error
        # etc.), so it lands on http_error. The old sink hardcoded
        # network_error and masked this; the shared primitive surfaces the
        # real code now.
        assert result.classification == "http_error"

    async def test_http_strategy_result_httpx_http_status_error(self):
        from app.crawl.cache import HttpCache

        cache = HttpCache()

        import httpx

        class MockResponse:
            content = b"error"
            status_code = 500
            headers = {"content-type": "text/plain"}
            url = "http://error.example.com/"

        unreachable_fetcher = failing_fetcher(
            httpx.HTTPStatusError("server error", request=None, response=MockResponse())
        )

        result = await run_http_strategy(
            url="http://error.example.com/",
            policy=http_strategy_policy(),
            http_fetcher=unreachable_fetcher,
            cache=cache,
        )
        assert result.classification == "http_status_error"

    def test_decode_body_unicode_decode_error(self):
        from app.crawl.strategies.body_decode import _decode_body

        body = b"\xff\xfe"
        result = _decode_body(body, "text/html")
        assert isinstance(result, str)

    async def test_http_strategy_result_binary_classification(self):
        from app.constants import EXTRACTION_METHOD_BINARY_NO_TEXT
        from app.crawl.cache import HttpCache

        cache = HttpCache()

        binary_fetcher = scripted_fetcher(
            status=200, headers={"content-type": "application/octet-stream"}, body=b"binary"
        )

        result = await run_http_strategy(
            url="http://binary.example.com/file.bin",
            policy=http_strategy_policy(),
            http_fetcher=binary_fetcher,
            cache=cache,
        )
        assert result.classification == EXTRACTION_METHOD_BINARY_NO_TEXT


async def test_http_strategy_rejects_non_mapping_request_profile():
    async def fetcher(*args, **kwargs):
        raise AssertionError("fetcher must not run for an invalid request profile")

    for profile in ("hello", ["hello"]):
        policy = http_strategy_policy()
        policy["request_profile"] = profile
        with pytest.raises(ValueError, match="^invalid_policy_profile$"):
            await run_http_strategy(
                url="http://profile.example/",
                policy=policy,
                http_fetcher=fetcher,
            )


async def test_stateful_request_profile_is_resolved_once_for_headers_and_diagnostics():
    from collections.abc import Mapping

    class StatefulProfile(Mapping[str, object]):
        def __init__(self):
            self.snapshot = 0

        def __iter__(self):
            self.snapshot += 1
            yield "user_agent_family"
            yield "cookie_jar"
            yield "cookies"

        def __len__(self):
            return 3

        def __getitem__(self, key):
            if self.snapshot == 1:
                values = {
                    "user_agent_family": "desktop_chrome",
                    "cookie_jar": "none",
                    "cookies": {},
                }
            else:
                values = {
                    "user_agent_family": "generic_crawler",
                    "cookie_jar": "per_host",
                    "cookies": {"profile.example": {"session": "second"}},
                }
            return values[key]

    profile = StatefulProfile()
    captured_headers: dict[str, str] = {}

    def respond(url, headers):
        captured_headers.update(headers)
        return (200, b"<html><body>profile content</body></html>", "text/html")

    fetcher = request_fetcher(respond)

    policy = http_strategy_policy()
    policy["request_profile"] = profile
    result = await run_http_strategy(
        url="http://profile.example/",
        policy=policy,
        http_fetcher=fetcher,
    )

    assert result.diagnostics["request_profile"] == {
        "user_agent_family": "desktop_chrome",
        "cookie_jar": "none",
    }
    assert "Cookie" not in captured_headers
    assert profile.snapshot == 1
