"""TestDecompressionCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestDecompressionCoverageGaps:
    """Cover remaining branches in decompression.py"""

    def test_decompress_body_brotli_import_error(self):
        from app.crawl.decompression import decompress_body
        body = b"not brotli"
        result = decompress_body(body, "br")
        assert result == body


