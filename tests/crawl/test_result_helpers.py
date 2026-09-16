"""Result helpers: blocked, binary, failed and skipped engine results."""

from _testkit import *


class TestResultHelpersEdges:
    """Cover remaining branches in result_helpers.py"""

    def test_blocked_engine_result(self):
        from app.crawl.result_helpers import _blocked_engine_result

        result = _blocked_engine_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_DECLARED_CRAWLER",
            classification="blocked",
            http_status=403,
        )
        assert result.status == "blocked"
        assert result.classification == "blocked"
        assert result.http_status == 403

    def test_binary_no_text_result(self):
        from app.crawl.result_helpers import _binary_no_text_result

        result = _binary_no_text_result(
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


class TestTerminalResultContract:
    """M1: the three terminal builders differ only in status and error defaulting.

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
        from app.crawl.result_helpers import _failed_engine_result

        result = _failed_engine_result(
            original_url="http://a/",
            normalized_url="http://a/",
            strategy="HTTP_DECLARED_CRAWLER",
            classification="network_error",
            error="boom",
            http_status=502,
            final_url="http://a/final",
            diagnostics={"k": "v"},
        )
        assert result.status == "failed"
        assert result.classification == "network_error"
        assert result.error == "boom"
        assert result.http_status == 502
        assert result.final_url == "http://a/final"
        assert result.diagnostics == {"k": "v"}
        assert result.html is None and result.text is None and result.content_type is None
        assert self._attempt_fields(result) == {
            "strategy": "HTTP_DECLARED_CRAWLER",
            "status": "failed",
            "classification": "network_error",
            "http_status": 502,
            "error": "boom",
            "content_type": None,
            "elapsed_ms": 0,
        }

    def test_blocked_engine_result_defaults_error_to_classification(self):
        from app.crawl.result_helpers import _blocked_engine_result

        result = _blocked_engine_result(
            original_url="http://b/",
            normalized_url="http://b/",
            strategy="BROWSER_HEADLESS",
            classification="captcha_detected",
            http_status=403,
        )
        assert result.status == "blocked"
        assert result.error == "captcha_detected"
        assert self._attempt_fields(result)["error"] == "captcha_detected"
        assert self._attempt_fields(result)["status"] == "blocked"

    def test_skipped_engine_result_never_carries_url_or_status(self):
        from app.crawl.result_helpers import _skipped_engine_result

        result = _skipped_engine_result(
            original_url="http://c/",
            normalized_url="http://c/",
            strategy="HTTP_DECLARED_CRAWLER",
            classification="robots_disallowed",
            error="robots",
        )
        assert result.status == "skipped"
        assert result.final_url is None
        assert result.http_status is None
        assert self._attempt_fields(result)["http_status"] is None
        assert self._attempt_fields(result)["status"] == "skipped"
