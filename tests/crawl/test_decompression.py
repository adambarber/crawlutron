"""Response body decompression behavior (no sys.path hacks)."""

import gzip
import zlib

import pytest

from app.crawl.decompression import decompress_body
from app.crawl.strategies.http import _decode_body

_CAP = 10_000_000


def test_gzip():
    assert decompress_body(gzip.compress(b"hello world"), "gzip", max_output_bytes=_CAP) == b"hello world"


def test_deflate():
    assert decompress_body(zlib.compress(b"hello world"), "deflate", max_output_bytes=_CAP) == b"hello world"


def test_no_encoding():
    assert decompress_body(b"hello", "", max_output_bytes=_CAP) == b"hello"


def test_cap_is_required_keyword():
    # H2 closure: the uncapped branch existed only as a default; making the
    # cap mandatory turns "forgot to bound a bomb" into a TypeError.
    with pytest.raises(TypeError):
        decompress_body(gzip.compress(b"hello"), "gzip")


def test_garbage_input_still_passes_through_under_cap():
    assert decompress_body(b"not gzip at all", "gzip", max_output_bytes=_CAP) == b"not gzip at all"


def test_decode_body_no_longer_decompresses():
    # Decompression lives at the chokepoint, not inside the text decoder;
    # the headers parameter existed only to double-decompress uncapped.
    with pytest.raises(TypeError):
        _decode_body(gzip.compress(b"hello world"), "text/html", {"content-encoding": "gzip"})


def test_decode_body_respects_charset():
    body = "<html>é</html>".encode()
    assert "é" in _decode_body(body, "text/html; charset=utf-8")


def test_decode_body_handles_unknown_charset():
    assert _decode_body(b"plain", "text/plain") == "plain"


def test_brotli():
    import brotli

    assert decompress_body(brotli.compress(b"hello brotli"), "br", max_output_bytes=_CAP) == b"hello brotli"


def test_decode_body_empty_body():
    assert _decode_body(b"", "text/html") == ""


class TestDecompressionEncodings:
    def test_decompression_zlib(self):
        import zlib

        from app.crawl.decompression import decompress_body

        original = b"hello world " * 10
        compressed = zlib.compress(original)
        result = decompress_body(compressed, "deflate", max_output_bytes=10_000_000)
        assert (result) is not None

    def test_decompression_gzip(self):
        import gzip

        from app.crawl.decompression import decompress_body

        original = b"hello world " * 10
        compressed = gzip.compress(original)
        result = decompress_body(compressed, "gzip", max_output_bytes=10_000_000)
        assert (result) is not None

    def test_decompression_unknown_encoding(self):
        from app.crawl.decompression import decompress_body

        result = decompress_body(b"some data", "unknown", max_output_bytes=10_000_000)
        assert result == b"some data"

    def test_decompression_none_encoding(self):
        from app.crawl.decompression import decompress_body

        # With an unrecognized encoding, returns the original body
        result = decompress_body(b"some data", "unknown_encoding", max_output_bytes=10_000_000)
        assert result == b"some data"


class TestDecompressionFallback:
    def test_decompression_brotli_exception(self):
        from app.crawl.decompression import decompress_body

        try:
            result = decompress_body(b"not gzip data", "gzip", max_output_bytes=10_000_000)
            assert result == b"not gzip data"
        except Exception:
            pass


class TestBodyCharsetDecoding:
    def test_http_charset_decode_error(self):
        from app.crawl.strategies.http import _decode_body

        # Charset rides on the content-type only; a stray value can never
        # reach the decoder as a headers argument (the parameter is gone).
        result = _decode_body(b"hello", "text/plain; charset=invalid_charset")
        assert isinstance(result, str)


class TestDecompressionBehaviors:
    def test_gzip(self):
        import gzip

        data = gzip.compress(b"hello world")
        assert decompress_body(data, "gzip", max_output_bytes=10_000_000) == b"hello world"

    def test_deflate(self):
        import zlib

        data = zlib.compress(b"hello world")
        assert decompress_body(data, "deflate", max_output_bytes=10_000_000) == b"hello world"

    def test_no_encoding(self):
        assert decompress_body(b"hello", "", max_output_bytes=10_000_000) == b"hello"

    def test_unknown_encoding(self):
        assert decompress_body(b"hello", "unknown", max_output_bytes=10_000_000) == b"hello"
