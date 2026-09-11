"""Per-encoding decompression failure handling and plain passthrough."""

from coverage_helpers import *


class TestDecompressionSelection:
    def test_gzip_failure_returns_body(self):
        assert decompress_body(b"not gzip", "gzip", max_output_bytes=10_000_000) == b"not gzip"

    def test_deflate_failure_returns_body(self):
        assert decompress_body(b"not deflate", "deflate", max_output_bytes=10_000_000) == b"not deflate"

    def test_brotli_failure_returns_body(self):
        assert decompress_body(b"not brotli", "br", max_output_bytes=10_000_000) == b"not brotli"

    def test_plain_passthrough(self):
        assert decompress_body(b"plain", "identity", max_output_bytes=10_000_000) == b"plain"
