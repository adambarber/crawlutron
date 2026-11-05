from __future__ import annotations

import sys
from typing import cast

import httpx
import pytest

from crawlutron.admission.policy import PolicyTier, read_policy, validate_wire_policy
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.http_fetch_result import HttpFetchResult as _HttpFetchResult
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.request_profile_record import RequestProfileRecord
from tests._fetch_doubles import FetcherDouble
from tests._fetch_outcome import FetchFailed
from tests._fetcher_factories import run_http_strategy
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

    def test_charset_from_bytes_import_error(self, monkeypatch):
        """Re-import with ``from_bytes`` missing from charset_normalizer so the
        module's real ImportError branch runs, then decode through the fallback."""
        import importlib
        import types

        from crawlutron.transport import body_decode

        real_module = importlib.import_module("charset_normalizer")
        real_models = importlib.import_module("charset_normalizer.models")
        # A package stub that keeps ``models`` importable but carries no ``from_bytes``:
        # ``from charset_normalizer import from_bytes`` raises ImportError -> None binding.
        stub = types.ModuleType("charset_normalizer")
        stub.__dict__["models"] = real_models
        monkeypatch.setitem(sys.modules, "charset_normalizer", stub)
        try:
            fresh = importlib.reload(body_decode)
            assert fresh._charset_from_bytes is None
            # Undecodable body: lossy UTF-8 replaces, nothing raises.
            result = fresh.decode_body(b"\xff\xfe x", "text/plain; charset=utf-8")
            assert result == "\ufffd\ufffd x"
            # The declared-charset fast path is untouched: valid bodies decode strictly.
            assert fresh.decode_body(b"ok", "text/plain; charset=utf-8") == "ok"
        finally:
            # Restore the real module before monkeypatch's undo so later tests get the
            # actual normalizer; swap it back and reload to drop the None binding.
            monkeypatch.undo()
            sys.modules["charset_normalizer"] = real_module
            importlib.reload(body_decode)

    async def test_close_async_http_client_error(self):
        from crawlutron.transport.http_client_pool import close_async_http_client

        await close_async_http_client()
        await close_async_http_client()

    def test_is_private_or_loopback_ip_invalid(self):
        from crawlutron.transport.pinned_dns import is_private_or_loopback_ip

        assert is_private_or_loopback_ip("127.0.0.1")
        assert is_private_or_loopback_ip("192.168.1.1")
        assert is_private_or_loopback_ip("10.0.0.1")
        assert is_private_or_loopback_ip("::1")
        assert not (is_private_or_loopback_ip("8.8.8.8"))
        assert is_private_or_loopback_ip("invalid")

    def test_impersonating_fetcher_response(self):
        """real_cffi subprocess: a normal fetch returns the served 200, isolated by an
        explicit start method -- a forked child inherits the suite-wide fetcher stub and
        fails only on CI. The queue's report is the production result, not a re-encoding.
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
        assert isinstance(outcome, _HttpFetchResult), f"cffi fetch failed in the subprocess: {outcome}"
        assert outcome.status_code == 200

    def test_the_isolated_fetch_reports_the_result_rather_than_its_status(self, monkeypatch):
        """A served page fetched by the real fetcher in its own interpreter: what comes
        back must BE the production result -- a re-encoding with an ``ok`` flag could not
        say which operator produced it and permitted states no producer ever meant.
        """
        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
        server = spawn_threaded_server(_Handler)
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            outcome = real_cffi_fetch_outcome(url, join_timeout=10.0)
        finally:
            server.shutdown()
            server.server_close()

        assert not isinstance(outcome, FetchFailed) and outcome.final_url is not None
        assert outcome.final_url.raw.startswith(NormalizedUrl(url, policy=HostPolicy.unrestricted()).raw), (
            f"cffi fetch produced {outcome!r}"
        )

    async def test_http_strategy_result_httpx_request_error(self):
        from crawlutron.transport.cache import HttpCache

        cache = HttpCache()

        unreachable_fetcher = FetcherDouble(
            lambda url, *_: httpx.RequestError("connection failed", request=None)
        )

        result = await run_http_strategy(
            url="http://fail.example.com/",
            policy=http_strategy_policy(),
            http_fetcher=unreachable_fetcher,
            cache=cache,
        )
        # A bare httpx.RequestError is the generic httpx-level tail of the classifier (more specific branches
        # map to connection_error/tls_error etc.), so it lands on http_error.
        assert result.classification == "http_error"

    async def test_http_strategy_result_httpx_http_status_error(self):
        from crawlutron.transport.cache import HttpCache

        cache = HttpCache()

        # A real request/response pair, because ``HTTPStatusError`` carries both and the strategy reads
        # ``.response`` off it.
        request = httpx.Request("GET", "http://error.example.com/")
        response = httpx.Response(
            500, headers={"content-type": "text/plain"}, content=b"error", request=request
        )

        unreachable_fetcher = FetcherDouble(
            lambda url, *_: httpx.HTTPStatusError("server error", request=request, response=response)
        )

        result = await run_http_strategy(
            url="http://error.example.com/",
            policy=http_strategy_policy(),
            http_fetcher=unreachable_fetcher,
            cache=cache,
        )
        assert result.classification == "http_status_error"

    def test_decode_body_unicode_decode_error(self):
        from crawlutron.transport.body_decode import decode_body

        body = b"\xff\xfe"
        result = decode_body(body, "text/html")
        assert isinstance(result, str)

    async def test_http_strategy_result_binary_classification(self):
        from crawlutron.transport.cache import HttpCache
        from crawlutron.vocabulary.extraction import EXTRACTION_METHOD_BINARY_NO_TEXT

        cache = HttpCache()

        binary_fetcher = FetcherDouble(
            lambda url, *_: {
                "status": 200,
                "headers": {"content-type": "application/octet-stream"},
                "body": b"binary",
            }
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

    # The rejecting edge, not ``read_policy``: a wire payload is validated by ``validate_wire_policy``, and
    # ``read_policy`` is total by contract -- it repairs a bad field with its default and never raises.
    for profile in ("hello", ["hello"]):
        with pytest.raises(ValueError, match="^invalid_policy_profile$"):
            bad_dump = http_strategy_policy().model_dump()
            bad_dump["request_profile"] = cast("JsonValue", profile)
            validate_wire_policy(bad_dump, PolicyTier.model_defaults())


async def test_stateful_request_profile_is_resolved_once_for_headers_and_diagnostics():
    from tests._profile_doubles import StatefulRequestProfile

    profile = StatefulRequestProfile()
    captured_headers: dict[str, str] = {}

    def respond(url, headers):
        captured_headers.update(headers)
        return (200, b"<html><body>profile content</body></html>", "text/html")

    fetcher = FetcherDouble(lambda url, sent_headers, *_: respond(url, sent_headers))

    profile_dump = http_strategy_policy().model_dump()
    profile_dump["request_profile"] = cast("RequestProfileRecord", profile)
    policy = read_policy(profile_dump)
    result = await run_http_strategy(
        url="http://profile.example/",
        policy=policy,
        http_fetcher=fetcher,
    )

    # One snapshot at the validation boundary: the model copies the caller's
    # mapping once, so headers and diagnostics can never disagree.
    assert result.diagnostics["request_profile"] == {
        "user_agent_family": "desktop_chrome",
        "cookie_jar": "none",
    }
    assert "generic_crawler" not in captured_headers["User-Agent"]
    assert "Cookie" not in captured_headers
    assert policy.request_profile == {
        "user_agent_family": "desktop_chrome",
        "cookie_jar": "none",
        "cookies": {},
    }
