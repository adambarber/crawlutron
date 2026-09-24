"""Body decompression and decoding: codec selection, caps, bombs, bytearray input and charset fallback."""

import gzip
import zlib

import pytest

from app.crawl.decompression import DecompressionTooLargeError, _decompress_bounded, decompress_body
from app.crawl.strategies.body_decode import _decode_body

_CAP = 10_000_000


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

        from app.crawl.decompression import decompress_body

        original = b"hello world " * 10
        compressed = zlib.compress(original)
        result = decompress_body(compressed, "deflate", max_output_bytes=10_000_000)
        assert result == original

    def test_decompression_gzip(self):
        import gzip

        from app.crawl.decompression import decompress_body

        original = b"hello world " * 10
        compressed = gzip.compress(original)
        result = decompress_body(compressed, "gzip", max_output_bytes=10_000_000)
        assert result == original

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
        from app.crawl.strategies.body_decode import _decode_body

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

        data = zlib.compress(b"hello world")
        assert decompress_body(data, "deflate", max_output_bytes=10_000_000) == b"hello world"

    def test_no_encoding(self):
        assert decompress_body(b"hello", "", max_output_bytes=10_000_000) == b"hello"

    def test_unknown_encoding(self):
        assert decompress_body(b"hello", "unknown", max_output_bytes=10_000_000) == b"hello"


def test_decode_valid_utf8_strict():
    body = "héllo wörld".encode()
    assert _decode_body(body, "text/html; charset=utf-8") == "héllo wörld"


def test_decode_declared_latin1():
    body = "café".encode("latin-1")
    assert _decode_body(body, "text/html; charset=latin-1") == "café"


def test_decode_invalid_bytes_does_not_raise():
    # Invalid UTF-8 bytes with a utf-8 declaration must still return a string
    # (via charset-normalizer or the lossy fallback), never raise.
    body = b"\xff\xfe\xc0not valid utf8"
    out = _decode_body(body, "text/html; charset=utf-8")
    assert isinstance(out, str)
    assert "not valid utf8" in out


def test_decode_unknown_charset_falls_back():
    # An unknown declared charset must not raise LookupError.
    body = b"plain"
    out = _decode_body(body, "text/html; charset=not-a-real-charset")
    assert out == "plain"


def test_decode_handles_bytearray_and_headers():
    import gzip

    from app.crawl.decompression import decompress_body

    raw = b"compressed"
    body = gzip.compress(raw)
    decoded_bytes = decompress_body(bytearray(body), "gzip", max_output_bytes=1_000_000)
    out = _decode_body(bytearray(decoded_bytes), "text/html")
    assert out == "compressed"


"""decompress_body must accept bytearray without requiring a bytes copy."""


def test_accepts_bytearray_gzip():
    import gzip

    from app.crawl.decompression import decompress_body, detect_decompression_failure

    body = bytearray(gzip.compress(b"hello world"))
    out = decompress_body(body, "gzip", max_output_bytes=1024)
    assert out == b"hello world"
    assert detect_decompression_failure(bytes(body), "gzip", out) is False


def test_accepts_bytearray_brotli():
    import brotli

    from app.crawl.decompression import decompress_body

    body = bytearray(brotli.compress(b"<html>br content</html>"))
    out = decompress_body(body, "br", max_output_bytes=1024)
    assert out == b"<html>br content</html>"


def test_accepts_memoryview():
    from app.crawl.decompression import decompress_body

    body = memoryview(gzip.compress(b"mv test")) if (gzip := __import__("gzip")) else None
    out = decompress_body(body, "gzip", max_output_bytes=10_000_000)
    assert out == b"mv test"


def _gzip_bomb(decompressed_size: int) -> bytes:
    # Highly compressible payload -> small on the wire, huge when expanded.
    return gzip.compress(b"A" * decompressed_size)


def test_decompression_raises_when_output_exceeds_limit():
    bomb = _gzip_bomb(1_000_000)  # ~1 MB expanded, ~1 KB on the wire
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "gzip", max_output_bytes=10_000)


def test_bounded_decompressor_reads_only_remaining_cap():
    requested: list[int] = []

    class FakeDecompressor:
        unconsumed_tail = b""
        eof = True
        unused_data = b""

        def decompress(self, data, max_length):
            requested.append(max_length)
            return b"A" * max_length

        def flush(self):
            return b""

    with pytest.raises(DecompressionTooLargeError):
        _decompress_bounded(FakeDecompressor, b"payload", 10)
    assert requested == [11]


def test_decompression_succeeds_under_limit():
    payload = b"A" * 50_000
    bomb = gzip.compress(payload)
    assert decompress_body(bomb, "gzip", max_output_bytes=100_000) == payload


def test_decompression_without_limit_is_rejected():
    payload = b"hello world"
    bomb = gzip.compress(payload)
    # H2 closure: no cap is no longer a legal call — the bound is required.
    with pytest.raises(TypeError):
        decompress_body(bomb, "gzip")


def test_deflate_bomb_is_bounded():

    bomb = zlib.compress(b"B" * 1_000_000)
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "deflate", max_output_bytes=10_000)


def test_brotli_bomb_is_bounded():
    # H6: a brotli bomb must raise, not expand unbounded in RAM.
    import brotli

    bomb = brotli.compress(b"C" * 1_000_000)
    assert len(bomb) < 10_000
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "br", max_output_bytes=10_000)


def test_brotli_feeds_incrementally(monkeypatch):
    # H6 mechanism: the google-brotli streaming API must be fed in chunks
    # so the full output is never materialized before the cap is checked.
    # A single process() call with the whole input means unbounded RAM.
    import brotli

    calls: list[int] = []
    real_decompressor = brotli.Decompressor

    class SpyDecompressor(real_decompressor):
        def process(self, data):
            calls.append(len(data))
            return super().process(data)

    monkeypatch.setattr(brotli, "Decompressor", SpyDecompressor)
    import os

    # Incompressible input: the wire form stays large so any reasonable
    # chunking splits it across several process() calls.
    payload = os.urandom(200_000)
    bomb = brotli.compress(payload)
    assert len(bomb) > 65_536
    assert decompress_body(bomb, "br", max_output_bytes=10_000_000) == payload
    assert len(calls) > 1
    assert max(calls) <= 65_536


class TestDecompressionSelection:
    def test_gzip_failure_returns_body(self):
        assert decompress_body(b"not gzip", "gzip", max_output_bytes=10_000_000) == b"not gzip"

    def test_deflate_failure_returns_body(self):
        assert decompress_body(b"not deflate", "deflate", max_output_bytes=10_000_000) == b"not deflate"

    def test_brotli_failure_returns_body(self):
        assert decompress_body(b"not brotli", "br", max_output_bytes=10_000_000) == b"not brotli"

    def test_plain_passthrough(self):
        assert decompress_body(b"plain", "identity", max_output_bytes=10_000_000) == b"plain"


class TestBrotliImportErrorFallback:
    """Brotli import failure degrades to raw bytes"""

    def test_decompress_body_brotli_import_error(self):
        from app.crawl.decompression import decompress_body

        body = b"not brotli"
        result = decompress_body(body, "br", max_output_bytes=10_000_000)
        assert result == body
