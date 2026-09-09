"""Decompression edge cases: bomb caps, chunked streams, corrupt payloads."""

from coverage_helpers import *


class TestDecompressionEdgeCases:
    """Cover remaining branches in decompression.py"""

    def test_decompress_body_brotli_import_error(self):
        from app.crawl.decompression import decompress_body

        body = b"not brotli"
        result = decompress_body(body, "br")
        assert result == body
