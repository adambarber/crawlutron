"""TestDecompressionGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestDecompressionGaps:
    def test_gzip_failure_returns_body(self):
        assert decompress_body(b"not gzip", "gzip") == b"not gzip"

    def test_deflate_failure_returns_body(self):
        assert decompress_body(b"not deflate", "deflate") == b"not deflate"

    def test_brotli_failure_returns_body(self):
        assert decompress_body(b"not brotli", "br") == b"not brotli"

    def test_plain_passthrough(self):
        assert decompress_body(b"plain", "identity") == b"plain"
