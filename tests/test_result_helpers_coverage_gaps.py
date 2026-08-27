"""TestResultHelpersCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestResultHelpersCoverageGaps:
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



