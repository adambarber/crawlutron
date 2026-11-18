"""Text format extraction: CSV, JSON, XML adversarial tests.

Matrix: 1M-row CSV over cap, embedded newlines, mixed line endings, deep JSON
nesting, JSONL, malformed JSON, XML with DTD (refused, not expanded).
"""

from __future__ import annotations

from crawlutron_extract import ExtractLimits, Quality, extract


def test_csv_basic():
    content = b"name,age\nAlice,30\nBob,25"
    result = extract(content, "text/csv")
    assert result.quality == Quality.MEDIUM
    assert "Alice" in result.text
    assert "Bob" in result.text
    assert result.metadata["rows"] == "3"


def test_csv_embedded_newlines():
    content = b'name,note\nAlice,"line1\nline2"\nBob,ok'
    result = extract(content, "text/csv")
    assert result.quality == Quality.MEDIUM
    assert "line1" in result.text
    assert "line2" in result.text


def test_csv_mixed_line_endings():
    content = b"a,b\r\nc,d\re,f\r\ng,h"
    result = extract(content, "text/csv")
    assert result.quality == Quality.MEDIUM
    assert result.metadata["rows"] == "4"


def test_csv_over_cap():
    rows = b"\n".join(b"col1,col2" for _ in range(1_000_001))
    content = b"col1,col2\n" + rows
    limits = ExtractLimits(max_table_rows=10)
    result = extract(content, "text/csv", limits=limits)
    assert len(result.tables) <= 10
    assert result.truncated
    assert "table_rows_capped" in result.flags


def test_json_basic():
    content = b'{"name": "Alice", "age": 30}'
    result = extract(content, "application/json")
    assert result.quality == Quality.MEDIUM
    assert "Alice" in result.text


def test_json_deep_nesting():
    content = b"[" * 60 + b"]" * 60
    result = extract(content, "application/json")
    assert "json_depth_capped" in result.flags
    assert result.quality == Quality.MEDIUM


def test_jsonl():
    content = b'{"id": 1}\n{"id": 2}\n{"id": 3}'
    result = extract(content, "application/jsonl")
    assert result.quality == Quality.MEDIUM
    assert '"id": 1' in result.text or "1" in result.text


def test_malformed_json():
    content = b"{not valid json}"
    result = extract(content, "application/json")
    assert result.quality == Quality.FAILED
    assert "json_parse_error" in result.flags


def test_xml_basic():
    content = b"<root><item>Hello</item><item>World</item></root>"
    result = extract(content, "application/xml")
    assert result.quality == Quality.MEDIUM
    assert "Hello" in result.text
    assert "World" in result.text


def test_xml_dtd_refused():
    content = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "bar">]><root>&xxe;</root>'
    result = extract(content, "application/xml")
    assert result.quality == Quality.FAILED
    assert "xml_dtd_refused" in result.flags
    assert result.text == ""


def test_xml_malformed():
    content = b"<root><unclosed>"
    result = extract(content, "application/xml")
    assert result.quality == Quality.FAILED
    assert "xml_parse_error" in result.flags


def test_xml_text_only():
    content = b"<root>Just some text</root>"
    result = extract(content, "application/xml")
    assert result.quality == Quality.MEDIUM
    assert "Just some text" in result.text


def test_unsupported_content_type():
    result = extract(b"hello", "image/png")
    assert result.quality == Quality.FAILED
    assert "format_refused" in result.flags


def test_text_format_determinism():
    content = b'{"name": "Test", "value": 42}'
    limits = ExtractLimits()
    a = extract(content, "application/json", limits=limits)
    b = extract(content, "application/json", limits=limits)
    assert a == b


def test_csv_empty():
    result = extract(b"", "text/csv")
    assert result.quality == Quality.FAILED


def test_json_empty():
    result = extract(b"", "application/json")
    assert result.quality == Quality.FAILED


def test_pdf_binary_container_quality_low():
    """Binary container (PDF) with no tier: empty text, container flag, quality=low."""
    result = extract(b"%PDF-1.4 fake pdf content", "application/pdf")
    assert result.quality == Quality.LOW
    assert result.text == ""
    assert result.markdown == ""
    assert "pdf_unsupported" in result.flags


def test_docx_binary_container_quality_low():
    """Binary container (DOCX) with corrupt content: empty text, corrupt flag, quality=failed."""
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    result = extract(b"fake docx content", ct)
    assert result.quality == Quality.FAILED
    assert result.text == ""
    assert result.markdown == ""
    assert "corrupt_container" in result.flags


def test_image_png_refused():
    """Truly unsupported type (image/png): FAILED with format_refused."""
    result = extract(b"fake png", "image/png")
    assert result.quality == Quality.FAILED
    assert "format_refused" in result.flags
    assert result.text == ""
