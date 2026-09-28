import multiprocessing
import os

import pytest
from _fetcher_factories import failing_fetcher, request_fetcher, run_http_strategy, scripted_fetcher
from _policy_factories import http_strategy_policy
from _transport_doubles import join_or_kill
from helpers.server import _Handler, spawn_threaded_server

from app.crawl.policy import PolicyTier, read_policy, validate_wire_policy


def _cffi_smoke_worker(url: str, out_queue: multiprocessing.Queue) -> None:
    """Subprocess body for the cffi smoke test (must be module-level to pickle)."""
    import asyncio

    os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"

    from app.crawl.strategies.fetchers import impersonating_fetcher
    from app.validation import HostPolicy

    # `impersonating_fetcher` is the factory the engine binds for the
    # impersonating strategy, so calling the factory is what makes this a test
    # of the shipped transport preference rather than of a second entry point.
    try:
        policy = HostPolicy.unrestricted()
        result = asyncio.run(impersonating_fetcher(policy)(url, {}, 10, 50 * 1024 * 1024, policy=policy))
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

    def test_impersonating_fetcher_response(self):
        """real_cffi subprocess: a normal fetch returns the served 200.

        In-process real cffi is banned (#1744/#1746): under fast_sleeps the
        curl watchdog busy-spins the GIL and wedges the pytest process, so
        the fetch runs in an isolated interpreter with a hard join.
        """
        import multiprocessing

        server = spawn_threaded_server(_Handler)
        url = f"http://127.0.0.1:{server.server_port}/"
        out_queue: multiprocessing.Queue = multiprocessing.Queue()
        worker = multiprocessing.Process(target=_cffi_smoke_worker, args=(url, out_queue))
        try:
            worker.start()
            join_or_kill(worker, 10.0, "the cffi fetcher on a normal response")
        finally:
            server.shutdown()
            server.server_close()

        assert worker.exitcode == 0, f"worker crashed: exitcode={worker.exitcode}"
        assert not out_queue.empty(), "worker produced no result"
        # The error branch reports three values, so unpacking positionally turned
        # a real failure into "too many values to unpack" and hid the reason the
        # subprocess died. Read the outcome first and name the rest.
        outcome, *detail = out_queue.get()
        assert outcome == "ok", f"cffi fetch failed in the subprocess: {detail}"
        assert detail == [200], detail

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

    # The rejecting edge, not ``read_policy``: a wire payload is validated by
    # ``validate_wire_policy``, and ``read_policy`` is total by contract -- it
    # repairs a bad field with its default and never raises. Asking the total
    # edge to reject is asking it to break its own contract, and the answer
    # would prove nothing about what a caller is told.
    for profile in ("hello", ["hello"]):
        with pytest.raises(ValueError, match="^invalid_policy_profile$"):
            policy = validate_wire_policy(
                {**http_strategy_policy().model_dump(), "request_profile": profile},
                PolicyTier.model_defaults(),
            )
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

    policy = read_policy({**http_strategy_policy().model_dump(), "request_profile": profile})
    result = await run_http_strategy(
        url="http://profile.example/",
        policy=policy,
        http_fetcher=fetcher,
    )

    # One snapshot, taken at the read edge. If anything re-read the profile the
    # StatefulProfile would answer "generic_crawler" the second time, and the
    # headers would disagree with the diagnostics -- which is the defect this
    # profile was built to make visible.
    assert result.diagnostics["request_profile"] == {
        "user_agent_family": "desktop_chrome",
        "cookie_jar": "none",
    }
    assert "generic_crawler" not in captured_headers["User-Agent"]
    assert "Cookie" not in captured_headers
    assert profile.snapshot == 1
