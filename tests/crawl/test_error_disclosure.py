"""Adversarial fix (HIGH): error responses must not leak raw exception
text (DNS names, internal paths, stack detail) in the API payload.

Raw detail is NEVER returned to the caller. Only a stable error code is exposed
in ``error``/``diagnostics``, and the raw detail is logged server-side for
debugging (see app.crawl.strategies.http.http_strategy_result).
"""

import json
import logging
import socket

import httpx

from app.crawl.decompression import DecompressionTooLargeError
from app.crawl.result_helpers import (
    classify_error_code,
    crawl_result_from_exception,
    engine_result_from_exception,
)
from app.models import to_dict
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


async def test_http_strategy_result_hides_raw_dns_error():
    res = await _run(socket.gaierror("[Errno -2] Name or service not known"))
    assert res.status == "failed"
    # Stable code, never the raw message. dns_error is its own label because a
    # name that never resolved is a distinct diagnosis from a refused connection,
    # and reporting one as the other is what left it a taxonomy member with no
    # producer.
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
    same operation at two layers -- an ``EngineCrawlResult`` and the render
    pipeline's ``CrawlResult`` -- and each used to spell out its own ``extra``
    dict. Nothing asserted the two agreed, so the drift was invisible from either
    side: an operator grepging the failure stream for the raw detail would have
    found the engine path, not found it on the render path, and concluded the
    render path had stopped logging the detail that makes it debuggable. One
    classifier-and-logger now serves both, and the event name is the only thing
    that differs.

    The parity assertion is ``vars()`` of the two records compared to each other,
    not to a hand-written key list: a snapshot of the key list passes whenever
    both paths are wrong in the same direction, and goes stale the first time
    someone legitimately adds a field to the shared payload. Comparing the two
    records states the property that is actually at stake, and the three keys
    that carry the redaction are then required by name on both.
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
    assert engine_record.funcName == render_record.funcName == "_classify_and_log"
    # Distinct event names, so the two layers stay tellable apart in the stream.
    assert {r.msg for r in caplog.records} == {"strategy_result_error", "render_result_error"}
    # The raw detail is in the log and not on the wire: that asymmetry is the
    # whole reason the field exists, on both paths.
    for record, result, expected_strategy in (
        (engine_record, engine, "HTTP_DECLARED_CRAWLER"),
        (render_record, render, "BROWSER_HEADLESS"),
    ):
        assert {"strategy", "error_code", "error_detail"} <= set(vars(record))
        assert record.error_detail in {"engine boom /app/engine", "render boom /app/render"}
        assert record.strategy == expected_strategy
        assert record.error_code == result.classification
        assert "/app/" not in json.dumps(to_dict(result), default=str)


# One representative exception per branch of classify_error_code, paired
# with the stable code that branch emits. Every one of these codes is stored
# verbatim into CrawlStrategyAttempt.classification.
_ERROR_CODE_CASES = (
    (DecompressionTooLargeError("big"), "payload_too_large"),
    (httpx.TooManyRedirects("loop"), "too_many_redirects"),
    (httpx.HTTPError("base httpx failure"), "http_error"),
    # Inverse cases: codes that were already valid enum members must stay that
    # way -- extending the taxonomy must not regress them.
    (httpx.ConnectError("down"), "connection_error"),
    (ValueError("bad"), "value_error"),
    (RuntimeError("unknown"), "network_error"),
)

# The three codes adds to the taxonomy: known-dead fetch outcomes that must
# land in the ONE status table (and as "failed", matching the status the
# strategy already reported) instead of falling through to the default.
_NEW_ERROR_CODES = (
    "payload_too_large",
    "too_many_redirects",
    "http_error",
)


class TestEmittedErrorCodesAreValidClassifications:
    """Every emitted error code must be a ``CrawlClassification`` member.

    The strategy layer stores the code straight into
    ``CrawlStrategyAttempt.classification``. The internal attempt model is a
    lenient dataclass (plain ``str``), so an invalid code sails through the
    fetcher -- and then rejects at response shaping, where the field is the
    strict ``CrawlClassification`` enum, failing the whole batch with a 422
    instead of just the one URL. A code that is a member shapes cleanly.
    """

    async def test_strategy_error_codes_shape_through_the_response_model(self):
        from app.api_responses import (
            StrategyAttempt,
        )

        for exc, expected_code in _ERROR_CODE_CASES:
            res = await _run(exc)
            assert res.status == "failed"
            attempt = res.attempted_strategies[0]
            assert attempt.classification == expected_code, (
                f"{type(exc).__name__} classified as {attempt.classification!r}, expected {expected_code!r}"
            )
            # The response-shaping gate: this is where a non-member code rejects
            # and 422s the whole batch.
            shaped = StrategyAttempt(
                strategy=attempt.strategy,
                status=attempt.status,
                classification=attempt.classification,
                elapsed_ms=attempt.elapsed_ms,
            )
            assert shaped.classification.value == expected_code

    def test_every_emitted_error_code_is_an_explicit_status_table_entry(self):
        """New codes integrate with the ONE status table, not a parallel map.

        An error code with no entry falls through to the ``blocked`` default,
        which is the wrong verdict for a fetch that is known-dead: it keeps the
        URL eligible for re-crawl as if it were an unclassified block, and it
        disagrees with the ``failed`` status the strategy already reported.
        """
        from app.constants import CLASSIFICATION_STATUS_BY_LABEL, status_for_classification

        for code in _NEW_ERROR_CODES:
            assert code in CLASSIFICATION_STATUS_BY_LABEL, (
                f"error code {code!r} has no status entry; it would fall through "
                "to the blocked default instead of the failed status the fetcher "
                "reported"
            )
            assert status_for_classification(code) == "failed"


class TestFailureDiagnosticsRedaction:
    """The FAILURE path dumped the raw ``request_profile`` (which can carry
    live cookies, fingerprint_seed, cookie_namespace) into diagnostics, while
    the SUCCESS path exports only ``user_agent_family`` + ``cookie_jar``.
    Diagnostics reach the API response, result store, persisted job.results and
    webhook payloads, so the failure path must emit the same redacted shape.
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
