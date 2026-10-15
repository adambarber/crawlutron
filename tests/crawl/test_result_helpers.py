"""Result helpers: blocked, binary, failed and skipped engine results."""

from tests._result_builders import skipped_result


class TestResultHelpersEdges:
    """Cover remaining branches in result_helpers.py"""

    def test_binary_no_text_result(self):
        from app.orchestration.engine_result import binary_no_text_result

        result = binary_no_text_result(
            original_url="http://x/file.bin",
            normalized_url="http://x/file.bin",
            final_url="http://x/file.bin",
            strategy="HTTP_DECLARED_CRAWLER",
            http_status=200,
            headers={"content-type": "application/octet-stream"},
            content_type="application/octet-stream",
            elapsed_ms=100,
        )
        assert result.classification == "binary_no_text"
        assert result.http_status == 200
        assert result.content_type == "application/octet-stream"


class TestFunnelDerivesAxes:
    """The funnel takes the attempt and derives the axes from it.

    The record's flat status, classification and error are the winning
    attempt's values, so the funnel accepts no second spelling of them: a
    caller cannot make the two records disagree about one outcome.
    """

    def test_the_funnel_derives_the_axis_values_from_the_attempt(self):
        from app.models.crawl_result import CrawlStrategyAttempt
        from app.orchestration.engine_result import build_engine_result

        attempt = CrawlStrategyAttempt(
            strategy="HTTP_DECLARED_CRAWLER",
            status="success",
            classification="rendered",
            http_status=200,
            content_type="text/html",
            elapsed_ms=12,
            error=None,
        )
        result = build_engine_result(
            original_url="http://a/",
            normalized_url="http://a/",
            final_url="http://a/final",
            attempt=attempt,
            http_status=200,
        )
        assert result.status == attempt.status
        assert result.classification == attempt.classification
        assert result.error == attempt.error


class TestTerminalResultContract:
    """The three terminal builders differ only in status and error defaulting.

    Pinning every wire-visible field makes the collapse of
    _failed/_blocked/_skipped_engine_result onto one primitive provably
    behavior-preserving: a field that moves silently is caught here.
    """

    @staticmethod
    def _attempt_fields(result):
        attempt = result.attempted_strategies[0]
        return {
            "strategy": attempt.strategy,
            "status": attempt.status,
            "classification": attempt.classification,
            "http_status": attempt.http_status,
            "error": attempt.error,
            "content_type": attempt.content_type,
            "elapsed_ms": attempt.elapsed_ms,
        }

    def test_failed_engine_result_fields(self):
        from app.orchestration.engine_result import (
            TerminalResult,
            failed_engine_result,
        )

        result = failed_engine_result(
            TerminalResult(
                original_url="http://a/",
                normalized_url="http://a/",
                strategy="HTTP_DECLARED_CRAWLER",
                classification="network_error",
                error="connection_error",
                http_status=502,
                final_url="http://a/final",
                diagnostics={"k": "v"},
            )
        )
        assert result.status == "failed"
        assert result.classification == "network_error"
        assert result.error == "connection_error"
        assert result.http_status == 502
        assert result.final_url == "http://a/final"
        assert result.diagnostics == {"k": "v"}
        assert result.html is None and result.text is None and result.content_type is None
        assert self._attempt_fields(result) == {
            "strategy": "HTTP_DECLARED_CRAWLER",
            "status": "failed",
            "classification": "network_error",
            "http_status": 502,
            "error": "connection_error",
            "content_type": None,
            "elapsed_ms": 0,
        }

    def test_skipped_engine_result_never_carries_url_or_status(self):
        result = skipped_result(
            "http://c/",
            "robots_disallowed",
            strategy="HTTP_DECLARED_CRAWLER",
            error="robots_txt_disallowed",
        )
        assert result.status == "skipped"
        assert result.final_url is None
        assert result.http_status is None
        assert self._attempt_fields(result)["http_status"] is None
        assert self._attempt_fields(result)["status"] == "skipped"


class TestTerminalCrawlResult:
    """The /v1/render wire terminal: no payload, and error is caller-supplied.

    The browser failure path, the render disallowed path, and the render
    worker-failure path share this shape. Pinning the fields proves the
    collapse onto one primitive is behavior-preserving and that the result
    never carries a payload (html/text/final_url are always None).
    """

    def test_no_payload_and_carries_error_verbatim(self):
        from app.orchestration.render_result import terminal_crawl_result

        result = terminal_crawl_result(
            url="http://x/page",
            status="failed",
            classification="site_error",
            attempts=2,
            elapsed_ms=42,
            domain="x",
            error="network_error",
        )
        assert result.url == "http://x/page"
        assert result.status == "failed"
        assert result.classification == "site_error"
        assert result.attempts == 2
        assert result.elapsed_ms == 42
        assert result.domain == "x"
        assert result.error == "network_error"
        # A terminal result never carries page payload.
        assert result.final_url is None
        assert result.html is None
        assert result.text is None
