"""Adversarial fix (HIGH): error responses must not leak raw exception text.

Only a stable error code is exposed in ``error``/``diagnostics``; the raw
DNs/paths/stack detail is logged server-side, never returned to the caller.
"""

import asyncio
import json
import logging
import socket
import ssl

import httpx

from app.models.serialization import to_dict
from app.orchestration.classification import classify_error_code
from app.orchestration.results import crawl_result_from_exception, engine_result_from_exception
from app.transport.decompression import DecompressionTooLargeError
from tests._fetcher_factories import invoke_http_strategy, run_http_strategy
from tests._policy_factories import http_strategy_policy


class _RaisingFetcher:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, url, headers, timeout, max_content_length):
        async def _raise():
            raise self.exc

        return _raise()


async def _run(exc):
    fetcher = _RaisingFetcher(exc)
    policy = {"limits": {}, "extraction": "html", "headers": {}}
    return await run_http_strategy(
        url="http://example.com/",
        policy=policy,
        http_fetcher=fetcher,
    )


def test_classify_error_codes_map_to_stable_values():
    assert classify_error_code(socket.gaierror("boom")) == "dns_error"
    assert classify_error_code(httpx.ConnectError("x")) == "connection_error"
    assert classify_error_code(ValueError("bad")) == "value_error"
    assert classify_error_code(DecompressionTooLargeError("big")) == "payload_too_large"
    # Unknown exception -> generic network_error, never the raw message.
    assert classify_error_code(RuntimeError("secret /app/x")) == "network_error"


def test_every_network_fault_reaches_a_terminal_code():
    """OSError is the base of every network fault: a degraded resolver raising a
    bare OSError (or socket.timeout) must classify terminally, not network_error."""
    for exc, expected in _NETWORK_FAULT_CASES:
        assert classify_error_code(exc) == expected, (
            f"{type(exc).__name__} fell to {classify_error_code(exc)!r}, expected {expected!r}"
        )


def test_specific_rows_win_over_the_oserror_base():
    """Order is the correctness property: gaierror/herror stay dns_error and ssl
    stays tls_error even though OSError is their base and also has a row."""
    assert classify_error_code(socket.gaierror("x")) == "dns_error"
    assert classify_error_code(socket.herror("x")) == "dns_error"
    assert classify_error_code(ssl.SSLError("x")) == "tls_error"
    assert classify_error_code(ConnectionResetError("x")) == "connection_error"


def test_resolver_budget_still_maps_dns_error():
    """The bounded resolver's verdict must not be swallowed by the TimeoutError
    or OSError rows: the request never got to navigate (#383/#405)."""
    from app.transport.pinned_transport import ResolverBudgetExceeded

    assert classify_error_code(ResolverBudgetExceeded("over budget")) == "dns_error"


def test_cancelled_error_is_never_a_network_fault():
    """asyncio.CancelledError inherits BaseException directly; neither the
    OSError nor the ValueError row may claim a cancellation."""
    assert not issubclass(asyncio.CancelledError, OSError)
    assert classify_error_code(asyncio.CancelledError()) == "network_error"


async def test_http_strategy_result_hides_raw_dns_error():
    res = await _run(socket.gaierror("[Errno -2] Name or service not known"))
    assert res.status == "failed"
    # Stable code, never the raw message.
    assert res.error == "dns_error"
    assert res.error != "[Errno -2] Name or service not known"
    # Raw exception text must NOT be returned anywhere in the API diagnostics.
    assert "error_detail" not in res.diagnostics
    serialized = json.dumps(res.diagnostics, default=str)
    assert "Name or service not known" not in serialized


async def test_http_strategy_result_hides_arbitrary_exception():
    res = await _run(RuntimeError("internal secret /app/secret/path"))
    assert res.status == "failed"
    assert res.error == "network_error"
    assert res.error != "/app/secret/path"
    assert "error_detail" not in res.diagnostics
    serialized = json.dumps(res.diagnostics, default=str)
    assert "/app/secret/path" not in serialized


def _record_for(caplog, event):
    records = [r for r in caplog.records if r.msg == event]
    assert len(records) == 1, f"expected exactly one {event} record, got {len(records)}"
    return records[0]


def test_both_failure_primitives_log_the_same_server_side_payload(caplog):
    """The redaction half of this file's contract, asserted where it lives.

    ``engine_result_from_exception`` and ``crawl_result_from_exception`` are the
    same operation at two layers, each spelling its own ``extra`` dict.
    """
    caplog.set_level(logging.WARNING)

    engine = engine_result_from_exception(
        error=RuntimeError("engine boom /app/engine"),
        original_url="https://example.com/a",
        normalized_url="https://example.com/a",
        strategy="HTTP_DECLARED_CRAWLER",
    )
    render = crawl_result_from_exception(
        error=RuntimeError("render boom /app/render"),
        url="https://example.com/b",
        domain="example.com",
        strategy="BROWSER_HEADLESS",
        elapsed_ms=7,
    )

    engine_record = _record_for(caplog, "strategy_result_error")
    render_record = _record_for(caplog, "render_result_error")
    # Same payload shape, from the same call site, differing only in the event.
    assert vars(engine_record).keys() == vars(render_record).keys()
    assert engine_record.funcName == render_record.funcName == "classify_and_log"
    # Distinct event names, so the two layers stay tellable apart in the stream.
    assert {r.msg for r in caplog.records} == {"strategy_result_error", "render_result_error"}
    # The raw detail is in the log and not on the wire: that asymmetry is the whole reason the field exists,
    # on both paths.
    for record, result, expected_strategy in (
        (engine_record, engine, "HTTP_DECLARED_CRAWLER"),
        (render_record, render, "BROWSER_HEADLESS"),
    ):
        assert {"strategy", "error_code", "error_detail"} <= set(vars(record))
        assert record.error_detail in {"engine boom /app/engine", "render boom /app/render"}
        assert record.strategy == expected_strategy
        assert record.error_code == result.classification
        assert "/app/" not in json.dumps(to_dict(result), default=str)


# One representative exception per branch of classify_error_code, paired with the stable code that branch
# emits. Every one of these codes is stored verbatim into CrawlStrategyAttempt.classification.
_ERROR_CODE_CASES = (
    (DecompressionTooLargeError("big"), "payload_too_large"),
    (httpx.TooManyRedirects("loop"), "too_many_redirects"),
    (httpx.HTTPError("base httpx failure"), "http_error"),
    # Inverse cases: codes that were already valid enum members must stay that way -- extending the taxonomy
    # must not regress them.
    (httpx.ConnectError("down"), "connection_error"),
    (ValueError("bad"), "value_error"),
    (RuntimeError("unknown"), "network_error"),
)

# A degraded resolver surfaces OSError-family faults no specific row claims; every
# network fault must reach a terminal code, never the network_error default.
_NETWORK_FAULT_CASES = (
    (TimeoutError("degraded resolver"), "connection_error"),
    (OSError("resolver died"), "connection_error"),
    (socket.gaierror("resolution refused"), "dns_error"),
    (ssl.SSLError("handshake failed"), "tls_error"),
)

_NEW_ERROR_CODES = (
    "payload_too_large",
    "too_many_redirects",
    "http_error",
)


class TestEmittedErrorCodesAreValidClassifications:
    """Every emitted error code must be a ``CrawlClassification`` member.

    The attempt model stores it as a lenient ``str``, so an invalid code sails
    through and then 422s the whole batch at shaping, where the field is strict.
    """

    async def test_strategy_error_codes_shape_through_the_response_model(self):
        from app.models.strategy_attempt import StrategyAttempt

        for exc, expected_code in _ERROR_CODE_CASES:
            res = await _run(exc)
            assert res.status == "failed"
            attempt = res.attempted_strategies[0]
            assert attempt.classification == expected_code, (
                f"{type(exc).__name__} classified as {attempt.classification!r}, expected {expected_code!r}"
            )
            # The response-shaping gate: this is where a non-member code rejects and 422s the whole batch.
            shaped = StrategyAttempt(
                strategy=attempt.strategy,
                status=attempt.status,
                classification=attempt.classification,
                elapsed_ms=attempt.elapsed_ms,
            )
            assert shaped.classification.value == expected_code

    def test_every_emitted_error_code_is_an_explicit_status_table_entry(self):
        """New codes integrate with the ONE status table, not a parallel map.

        A code with no entry falls through to ``blocked``, the wrong verdict
        for a known-dead fetch -- keeping it eligible for re-crawl.
        """
        from app.vocabulary.taxonomy import CLASSIFICATION_STATUS_BY_LABEL, status_for_classification

        for code in _NEW_ERROR_CODES:
            assert code in CLASSIFICATION_STATUS_BY_LABEL, (
                f"error code {code!r} has no status entry; it would fall through "
                "to the blocked default instead of the failed status the fetcher "
                "reported"
            )
            assert status_for_classification(code) == "failed"


class TestFailureDiagnosticsRedaction:
    """The FAILURE path dumped the raw ``request_profile`` into diagnostics.

    It can carry live cookies and reach API responses and webhook payloads,
    so it must match the redacted shape the SUCCESS path exports.
    """

    async def test_failure_diagnostics_redact_request_profile(self, monkeypatch):

        # Seed the profile with exactly the sensitive fields the leak exposes.
        policy = http_strategy_policy().model_copy(
            update={
                "request_profile": {
                    "user_agent_family": "chrome",
                    "cookie_jar": "session-jar",
                    "cookies": {"session": "SECRET-TOKEN", "csrf": "abc"},
                    "fingerprint_seed": 12345,
                    "cookie_namespace": "tenant-secret",
                }
            }
        )

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
