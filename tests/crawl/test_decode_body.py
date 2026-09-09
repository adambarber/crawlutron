"""#5: _decode_body decodes via a strict fast path with charset-normalizer fallback."""

from app.crawl.strategies.http import _decode_body


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

    raw = b"compressed"
    body = gzip.compress(raw)
    out = _decode_body(bytearray(body), "text/html", {"content-encoding": "gzip"})
    assert out == "compressed"
