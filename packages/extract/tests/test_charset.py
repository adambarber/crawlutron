"""Charset ladder: BOM -> Content-Type -> <meta charset> -> strict UTF-8 -> lossy.

Never raises on hostile input; reports resolved charset and quality.
"""

from __future__ import annotations

from crawlutron_extract import ExtractLimits, Quality, extract
from crawlutron_extract.charset import _detect_charset, decode_content


def test_utf8_bom_is_detected_and_stripped():
    content = b"\xef\xbb\xbfhello"
    text, charset, _flags = decode_content(content, "text/plain")
    assert text == "hello"
    assert charset == "utf-8"


def test_utf16_le_bom_is_detected_and_stripped():
    content = b"\xff\xfeh\x00e\x00l\x00l\x00o\x00"
    text, charset, _flags = decode_content(content, "text/plain")
    assert text == "hello"
    assert charset == "utf-16-le"


def test_utf16_be_bom_is_detected_and_stripped():
    content = b"\xfe\xff\x00h\x00e\x00l\x00l\x00o"
    text, charset, _flags = decode_content(content, "text/plain")
    assert text == "hello"
    assert charset == "utf-16-be"


def test_content_type_charset_is_honoured():
    # '\xe1' is 'a' acute in iso-8859-1.
    text, charset, _flags = decode_content(b"ol\xe1", "text/plain; charset=iso-8859-1")
    assert text == "ol\u00e1"
    assert charset == "iso-8859-1"


def test_content_type_charset_with_quotes():
    text, charset, _flags = decode_content(b"hello", 'text/html; charset="utf-8"')
    assert text == "hello"
    assert charset == "utf-8"


def test_content_type_charset_without_quotes():
    text, charset, _flags = decode_content(b"hello", "text/html;charset=utf-8")
    assert text == "hello"
    assert charset == "utf-8"


def test_html_meta_charset_is_detected():
    content = b'<html><head><meta charset="utf-8"></head><body>hi</body></html>'
    text, charset, _flags = decode_content(content, "text/html")
    assert charset == "utf-8"


def test_html_meta_http_equiv_charset_is_detected():
    content = (
        b'<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1"></head></html>'
    )
    text, charset, _flags = decode_content(content, "text/html")
    assert charset == "iso-8859-1"


def test_defaults_to_utf_8_when_no_charset_declared():
    text, charset, _flags = decode_content(b"hello", "text/plain")
    assert text == "hello"
    assert charset == "utf-8"


def test_invalid_utf_8_falls_back_lossy_with_flag():
    content = b"\xff\xfeinvalid"
    text, charset, flags = decode_content(content, "text/plain")
    assert "charset_lossy" in flags
    assert isinstance(text, str)


def test_overlong_utf8_form_falls_back_lossy():
    # Overlong encoding of 'A' (0x41) as 0xE0 0x80 0x41
    content = b"\xe0\x80\x41"
    text, charset, flags = decode_content(content, "text/plain")
    assert "charset_lossy" in flags


def test_lone_surrogate_bytes_fall_back_lossy():
    content = b"\xed\xa0\x80"
    text, charset, flags = decode_content(content, "text/plain")
    assert "charset_lossy" in flags


def test_unknown_charset_falls_back_lossy():
    text, charset, flags = decode_content(b"hello", "text/plain; charset=xyzzy")
    assert "charset_lossy" in flags
    assert isinstance(text, str)


def test_lying_charset_header_does_not_raise():
    # Declares iso-8859-1 but content is valid UTF-8 with multi-byte chars.
    content = "ol\u00e1".encode("utf-8")
    text, charset, _flags = decode_content(content, "text/plain; charset=iso-8859-1")
    assert isinstance(text, str)
    assert charset == "iso-8859-1"


def test_empty_content_decodes_to_empty():
    text, charset, _flags = decode_content(b"", "text/plain")
    assert text == ""
    assert charset == "utf-8"


def test_extract_reports_charset_in_metadata():
    result = extract(b"\xef\xbb\xbfhello", "text/plain; charset=utf-8")
    assert result.metadata["charset"] == "utf-8"
    assert result.text == "hello"


def test_extract_charset_field_matches_resolved():
    result = extract(b"hello", "text/plain")
    assert result.charset == "utf-8"


def test_clean_decode_sets_quality_low():
    """No extractor has run yet, so a clean non-refused decode is LOW."""
    result = extract(b"hello", "text/plain")
    assert result.quality == Quality.LOW
    assert result.flags == []


def test_lossy_decode_still_quality_low():
    """Lossy decode is also LOW; decode facts live in flags, not quality."""
    content = b"\xff\xfeinvalid"
    result = extract(content, "text/plain")
    assert result.quality == Quality.LOW
    assert "charset_lossy" in result.flags


def test_every_byte_value_decodes_without_raising():
    """Totality proof: every single byte value 0x00-0xFF decodes without
    exception. Non-UTF-8 bytes carry the charset_lossy flag."""
    body = bytes(range(256))
    text, charset, flags = decode_content(body, "text/plain")
    assert isinstance(text, str)
    assert charset == "utf-8"
    # Bytes above 0x7F are not valid standalone UTF-8, so they are lossy.
    assert "charset_lossy" in flags


def test_input_above_cap_refused_before_decode():
    limits = ExtractLimits(max_input_bytes=3)
    result = extract(b"hello", "text/plain", limits=limits)
    assert result.quality == Quality.FAILED
    assert "input_refused" in result.flags
    assert result.text == ""


def test_input_at_exact_cap_not_refused():
    limits = ExtractLimits(max_input_bytes=5)
    result = extract(b"hello", "text/plain", limits=limits)
    assert result.quality != Quality.FAILED or "input_refused" not in result.flags


def test_detect_charset_priority_bom_over_content_type():
    # UTF-8 BOM present; Content-Type declares iso-8859-1. BOM wins.
    content = b"\xef\xbb\xbf" + "ol\u00e1".encode("utf-8")
    charset = _detect_charset(content, "text/plain; charset=iso-8859-1")
    assert charset == "utf-8"
