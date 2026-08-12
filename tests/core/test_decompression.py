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
sys.path.insert(0, str(CRAWLER_APP))
sys.path.insert(0, str(ROOT / "docker" / "shared" / "python"))

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
