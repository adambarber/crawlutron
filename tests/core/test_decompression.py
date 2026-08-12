"""
Tests for response body decompression based on Content-Encoding header.
Prevents regressions from Accept-Encoding header changes.
"""
import gzip
import zlib
import sys
import pytest
from pathlib import Path

# Add app directory to path like other tests
ROOT = Path(__file__).resolve().parents[2]
CRAWLER_APP = ROOT / "docker" / "browser-crawler"

from app.service import _decompress_body, _decode_body

class TestDecompressBody:
    """Test _decompress_body handles various encodings."""

    def test_gzip_decompression(self):
        """Test gzip decompression works correctly."""
        original = b"Hello, World! This is a test body."
        compressed = gzip.compress(original)

        result = _decompress_body(compressed, "gzip")
        assert result == original

    def test_deflate_decompression(self):
        """Test deflate decompression works correctly."""
        original = b"Hello, World! This is a test body."
        compressed = zlib.compress(original)

        result = _decompress_body(compressed, "deflate")
        assert result == original

    def test_br_decompression(self):
        """Test brotli decompression works when brotli is available."""
        try:
            import brotli
            original = b"Hello, World! This is a test body."
            compressed = brotli.compress(original)

            result = _decompress_body(compressed, "br")
            assert result == original
        except ImportError:
            pytest.skip("brotli not installed")

    def test_no_encoding_returns_as_is(self):
        """Test that no encoding returns the body as-is."""
        original = b"Hello, World! This is a test body."

        result = _decompress_body(original, "")
        assert result == original

    def test_unknown_encoding_returns_as_is(self):
        """Test that unknown encoding returns the body as-is."""
        original = b"Hello, World! This is a test body."

        result = _decompress_body(original, "unknown")
        assert result == original

    def test_corrupt_gzip_returns_body_as_is(self):
        """curl_cffi auto-decompresses a gzip body but may leave the
        Content-Encoding: gzip header on the response; a non-gzip body labeled
        gzip must pass through unchanged (regression guard)."""
        already_decompressed = b"<!doctype html><html><body>mass.gov content</body></html>"

        result = _decompress_body(already_decompressed, "gzip")
        assert result == already_decompressed

    def test_corrupt_brotli_returns_body_as_is(self):
        """A body labeled br that is not valid brotli must pass through unchanged."""
        not_brotli = b"not valid brotli bytes"

        result = _decompress_body(not_brotli, "br")
        assert result == not_brotli

    def test_corrupt_deflate_returns_body_as_is(self):
        """A body labeled deflate that is not a valid zlib stream must pass
        through unchanged rather than raising."""
        not_deflate = b"<!doctype html><html><body>not zlib</body></html>"

        result = _decompress_body(not_deflate, "deflate")
        assert result == not_deflate

class TestDecodeBody:
    """Test _decode_body integrates with decompression."""

    def test_gzip_with_content_type(self):
        """Test _decode_body handles gzip with content type."""
        original = b"Hello, World! This is a test body."
        compressed = gzip.compress(original)

        result = _decode_body(compressed, "text/html", {"content-encoding": "gzip"})
        assert result == original.decode("utf-8")

    def test_deflate_with_content_type(self):
        """Test _decode_body handles deflate with content type."""
        original = b"Hello, World! This is a test body."
        compressed = zlib.compress(original)

        result = _decode_body(compressed, "text/html", {"content-encoding": "deflate"})
        assert result == original.decode("utf-8")

    def test_no_encoding_no_headers(self):
        """Test _decode_body without headers just decodes directly."""
        original = b"Hello, World! This is a test body."

        result = _decode_body(original, "text/html")
        assert result == original.decode("utf-8")

    def test_unknown_encoding_no_decompression(self):
        """Test that unknown encoding doesn't decompress."""
        original = b"Hello, World! This is a test body."

        result = _decode_body(original, "text/html", {"content-encoding": "unknown"})
        assert result == original.decode("utf-8")
