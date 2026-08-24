"""Response body decompression behavior (no sys.path hacks)."""

import gzip
import zlib

from app.crawl.decompression import decompress_body
from app.crawl.strategies.http import _decode_body


def test_gzip():
    assert decompress_body(gzip.compress(b"hello world"), "gzip") == b"hello world"


def test_deflate():
    assert decompress_body(zlib.compress(b"hello world"), "deflate") == b"hello world"


def test_no_encoding():
    assert decompress_body(b"hello", "") == b"hello"


def test_decode_body_respects_charset():
    body = "<html>é</html>".encode("utf-8")
    assert "é" in _decode_body(body, "text/html; charset=utf-8")


def test_decode_body_handles_unknown_charset():
    assert _decode_body(b"plain", "text/plain") == "plain"


def test_brotli():
    import brotli

    assert decompress_body(brotli.compress(b"hello brotli"), "br") == b"hello brotli"


def test_decode_body_empty_body():
    assert _decode_body(b"", "text/html") == ""
