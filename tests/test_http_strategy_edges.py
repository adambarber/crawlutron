import httpx
import pytest

from app.crawl.policy import PolicyTier, read_policy, validate_wire_policy
from app.models.host_policy import HostPolicy
from app.models.http_fetch_result import HttpFetchResult
from app.models.normalized_url import NormalizedUrl
from tests._fetcher_factories import failing_fetcher, request_fetcher, run_http_strategy, scripted_fetcher
from tests._policy_factories import http_strategy_policy
from tests._transport_doubles import (
    isolated_process,
    isolated_queue,
    join_or_kill,
    outcome_worker,
    real_cffi_fetch_outcome,
)
from tests.helpers.server import _Handler, spawn_threaded_server


class TestHttpStrategyEdges:
    """Cover remaining branches in http.py"""

    def test_charset_from_bytes_import_error(self):
        from app.crawl.strategies.body_decode import decode_body

        body = b"test content"
        result = decode_body(body, "text/plain; charset=utf-8")
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

        In-process real cffi is banned because the curl watchdog busy-spins the
        GIL under the suite's sleep patch; ``tests/_transport_doubles.py`` owns
        the arrangement and says why. What is asserted here is that the report
        arriving on the queue is the production result.

        The isolation is the load-bearing part, and it has to be asked for. A
        default ``multiprocessing.Process`` forks on Linux, and a forked child
        inherits the suite-wide ``impersonating_fetcher`` stub, so this test
        received the stub instead of the transport it is about and failed with
        "curl_cffi stubbed in tests" -- on CI only, having passed on the macOS
        where the same default spawns. ``isolated_process`` names the start
        method so the test means the same thing on both.

        This drove its own reporter, which put a ``("ok", 200)`` tuple on the
        queue; the harness reporter beside it put a record with an ``ok`` flag.
        Both were ways of not saying what came back, and the reader had to be
        told which slot was real. It uses the harness reporter now.
        """
        server = spawn_threaded_server(_Handler)
        url = f"http://127.0.0.1:{server.server_port}/"
        out_queue = isolated_queue()
        worker = isolated_process(outcome_worker, (url, 10, 50 * 1024 * 1024, out_queue))
        try:
            worker.start()
            join_or_kill(worker, 10.0, "the cffi fetcher on a normal response")
        finally:
            server.shutdown()
            server.server_close()

        assert worker.exitcode == 0, f"worker crashed: exitcode={worker.exitcode}"
        assert not out_queue.empty(), "worker produced no result"
        outcome = out_queue.get()
        assert isinstance(outcome, HttpFetchResult), f"cffi fetch failed in the subprocess: {outcome}"
        assert outcome.status_code == 200

    def test_the_isolated_fetch_reports_the_result_rather_than_its_status(self, monkeypatch):
        """A served page, fetched by the real fetcher in its own interpreter.

        What comes back has to BE the production result. It used to be a
        re-encoding of one -- an ``ok`` flag beside a ``status`` int -- and a
        re-encoding cannot say which operator produced it: the httpx fallback
        runs after the impersonated attempt precisely when there was none, so
        the same integer means both. It also permitted states no producer ever
        meant, ok-with-no-status and not-ok-with-a-status, which is what a hand-
        written union of primitives always permits.

        Asserting on a field the old record did not carry is the point: a status
        proves nothing about the result the fetcher actually produced.

        In-process real cffi is banned because the curl watchdog busy-spins the
        GIL under the suite's sleep patch; ``tests/_transport_doubles.py`` owns
        the arrangement and says why.
        """
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
        server = spawn_threaded_server(_Handler)
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            outcome = real_cffi_fetch_outcome(url, join_timeout=10.0)
        finally:
            server.shutdown()
            server.server_close()

        assert outcome.final_url.raw.startswith(
            NormalizedUrl(url, policy=HostPolicy.unrestricted()).raw
        ), f"cffi fetch produced {outcome!r}"

    async def test_http_strategy_result_httpx_request_error(self):
        from app.crawl.cache import HttpCache

        cache = HttpCache()

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

        # A real request/response pair, because ``HTTPStatusError`` carries both
        # and the strategy reads ``.response`` off it. Stand-ins left ``request``
        # as ``None`` and the response as an object that was not a response, so
        # the error a test built was not the error httpx raises.
        request = httpx.Request("GET", "http://error.example.com/")
        response = httpx.Response(
            500, headers={"content-type": "text/plain"}, content=b"error", request=request
        )

        unreachable_fetcher = failing_fetcher(
            httpx.HTTPStatusError("server error", request=request, response=response)
        )

        result = await run_http_strategy(
            url="http://error.example.com/",
            policy=http_strategy_policy(),
            http_fetcher=unreachable_fetcher,
            cache=cache,
        )
        assert result.classification == "http_status_error"

    def test_decode_body_unicode_decode_error(self):
        from app.crawl.strategies.body_decode import decode_body

        body = b"\xff\xfe"
        result = decode_body(body, "text/html")
        assert isinstance(result, str)

    async def test_http_strategy_result_binary_classification(self):
        from app.crawl.cache import HttpCache
        from app.vocabulary.extraction import EXTRACTION_METHOD_BINARY_NO_TEXT

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
