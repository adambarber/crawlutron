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
