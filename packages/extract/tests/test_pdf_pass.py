"""PDF tier-1 text sweep adversarial tests."""

from __future__ import annotations

import zlib

from crawlutron_extract import ExtractLimits, Quality, extract
from crawlutron_extract.limits import RICH_TEXT_FLOOR


def _stream(body: str) -> bytes:
    return b"stream\r\n" + zlib.compress(body.encode("utf-8")) + b"\rendstream"


def _make_pdf(*streams: str) -> bytes:
    parts = [b"%PDF-1.4\n"]
    for index, body in enumerate(streams, start=1):
        data = _stream(body)
        parts.append(b"%d 0 obj\n<< /Length %d >>\n" % (index, len(data)) + data + b"endobj\n")
    parts.append(b"%%EOF\n")
    return b"".join(parts)


def test_pdf_basic_text():
    """A Tj string in a stream extracts as text, quality=medium."""
    result = extract(_make_pdf("(Hello world) Tj"), "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "Hello world" in result.text


def test_pdf_tj_array_kerning_spacing():
    """[(A)(B)] TJ joins with spacing."""
    result = extract(_make_pdf("[(A)(B)] TJ"), "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "A B" in result.text


def test_pdf_newline_operators():
    """' and \" go to the next line before showing the string."""
    result = extract(_make_pdf("(a) '(b) '(c)"), "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "\na\nb" in result.text


def test_pdf_garbled_below_floor():
    """Mostly non-printable stream is below the floor: empty + partial flag."""
    body = "(" + chr(0) + chr(1) + chr(2) + chr(3) + chr(4) + ") Tj"
    result = extract(_make_pdf(body), "application/pdf")
    assert result.quality == Quality.LOW
    assert result.text == ""
    assert "pdf_encoding_partial" in result.flags


def test_pdf_control_chars_stripped():
    """Control characters are stripped from emitted text."""
    body = "(Hello" + chr(0) + chr(1) + "World) Tj"
    result = extract(_make_pdf(body), "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "HelloWorld" in result.text
    assert "\x00" not in result.text
    assert "\x01" not in result.text


def test_pdf_escaped_parens():
    """Escaped parens inside a literal string come through literally."""
    result = extract(_make_pdf("(Line \\(with\\) parens) Tj"), "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "Line (with) parens" in result.text


def test_pdf_hex_string():
    """A hex string operand decodes to text."""
    result = extract(_make_pdf("<48656c6c6f> Tj"), "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "Hello" in result.text


def test_pdf_huge_kerning_array_does_not_crash():
    """A TJ array with many numeric spacing elements is bounded."""
    body = "[(" + ")( ".join("x" * 500 for _ in range(200)) + ")] TJ"
    result = extract(_make_pdf(body), "application/pdf")
    assert isinstance(result.text, str)
    assert result.text == "" or len(result.text) <= ExtractLimits().max_text_chars


def test_pdf_empty():
    result = extract(b"", "application/pdf")
    assert result.quality == Quality.LOW
    assert result.text == ""
    assert "pdf_encoding_partial" in result.flags


def test_pdf_determinism():
    pdf = _make_pdf("(Deterministic) Tj")
    limits = ExtractLimits()
    a = extract(pdf, "application/pdf", limits=limits)
    b = extract(pdf, "application/pdf", limits=limits)
    assert a == b


def test_pdf_uncompressed_stream():
    """An uncompressed content stream still inflates and yields its text."""
    pdf = b"%PDF-1.4\nstream\nBT (Hello) Tj ET\nendstream\n%%EOF"
    result = extract(pdf, "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert result.text == "Hello"


def test_pdf_clean_page_reaches_medium():
    """A clean single-page document reaches medium quality, never low."""
    pdf = b"%PDF-1.4\nstream\nBT (Hello world) Tj ET\nendstream\n%%EOF"
    result = extract(pdf, "application/pdf")
    assert result.quality == Quality.MEDIUM
    assert "Hello world" in result.text


def test_pdf_clean_long_page_reaches_high():
    """A clean page above the rich-text floor reaches high quality."""
    body = "x" * RICH_TEXT_FLOOR
    pdf = b"%PDF-1.4\nstream\nBT (" + body.encode() + b") Tj ET\nendstream\n%%EOF"
    result = extract(pdf, "application/pdf")
    assert result.quality == Quality.HIGH
    assert body in result.text
