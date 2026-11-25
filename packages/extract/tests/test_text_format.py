"""Text format extraction: CSV, JSON, XML adversarial tests.

Matrix: 1M-row CSV over cap, embedded newlines, mixed line endings, deep JSON
nesting, JSONL, malformed JSON, XML with DTD (refused, not expanded).
"""

from __future__ import annotations

from typing import cast

from crawlutron_extract import ExtractLimits, Quality, extract
from crawlutron_extract.text_format import extract_text_format


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
    assert result.quality == Quality.FAILED
    assert "json_depth_refused" in result.flags


def test_json_2000_deep_refused_never_raises():
    payload = "[" * 2000 + "0" + "]" * 2000
    result = extract_text_format(payload, "application/json", ExtractLimits())
    assert result.get("_quality", "failed") == "failed"
    assert "json_depth_refused" in cast(list[str], result["quality_flags"])


def test_json_depth_boundary_49_50_51():
    for n, expected in ((49, "medium"), (50, "medium"), (51, "failed")):
        payload = "[" * n + "0" + "]" * n
        result = extract_text_format(payload, "application/json", ExtractLimits())
        if expected == "failed":
            assert result.get("_quality", "failed") == "failed"
            assert "json_depth_refused" in cast(list[str], result["quality_flags"])
        else:
            assert result.get("_quality", "failed") == "medium"


def test_json_brackets_inside_string_not_refused():
    payload = '{"a": "[["}'
    result = extract_text_format(payload, "application/json", ExtractLimits())
    assert result.get("_quality", "failed") == "medium"


def test_jsonl_deep_line_refused():
    payload = '{"id": 1}\n' + "[" * 2000 + "0" + "]" * 2000
    result = extract_text_format(payload, "application/jsonl", ExtractLimits())
    assert result.get("_quality", "failed") == "failed"


def test_jsonl():
    content = b'{"id": 1}\n{"id": 2}\n{"id": 3}'
    result = extract(content, "application/jsonl")
    assert result.quality == Quality.MEDIUM
    assert '"id": 1' in result.text or "1" in result.text


def test_json_input_capped_refused_before_jsonl_split():
    """Oversized JSON input is refused before the JSONL line-by-line re-split,
    so a body above the byte cap cannot be admitted as many small lines."""
    limits = ExtractLimits(max_input_bytes=5)
    payload = '{"a": 1}'
    result = extract_text_format(payload, "application/json", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "json_input_capped" in cast(list[str], result["quality_flags"])


def test_json_input_at_exact_cap_admitted():
    """Boundary: JSON input exactly at max_input_bytes is admitted."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("false", "application/json", limits)
    assert result.get("_quality", "failed") != "failed"
    assert "json_input_capped" not in cast(list[str], result["quality_flags"])


def test_json_input_one_byte_over_cap_refused():
    """Boundary: JSON input one byte over max_input_bytes is refused."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format('{"":0}', "application/json", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "json_input_capped" in cast(list[str], result["quality_flags"])


def test_csv_input_over_byte_cap_refused():
    """CSV input above max_input_bytes counts UTF-8 bytes and is refused."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("a,b,c,d,e", "text/csv", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "csv_input_capped" in cast(list[str], result["quality_flags"])


def test_csv_input_at_exact_byte_cap_admitted():
    """Boundary: CSV input exactly at max_input_bytes is admitted."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("a,bc\n", "text/csv", limits)
    assert result.get("_quality", "failed") != "failed"
    assert "csv_input_capped" not in cast(list[str], result["quality_flags"])


def test_csv_input_one_byte_over_byte_cap_refused():
    """Boundary: CSV input one byte over max_input_bytes is refused."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("a,bcd\n", "text/csv", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "csv_input_capped" in cast(list[str], result["quality_flags"])


def test_csv_byte_cap_counts_multibyte_utf8():
    """Byte cap counts UTF-8 bytes: 3 chars = 6 bytes exceeds a 5-byte cap."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("é,é\n", "text/csv", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "csv_input_capped" in cast(list[str], result["quality_flags"])


def test_xml_input_over_byte_cap_refused():
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("<a>hello</a>", "application/xml", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "xml_input_capped" in cast(list[str], result["quality_flags"])


def test_xml_input_at_exact_byte_cap_admitted():
    """Boundary: XML input exactly at max_input_bytes is admitted."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("<ab/>", "application/xml", limits)
    assert result.get("_quality", "failed") != "failed"
    assert "xml_input_capped" not in cast(list[str], result["quality_flags"])


def test_xml_input_one_byte_over_byte_cap_refused():
    limits = ExtractLimits(max_input_bytes=5)
    result = extract_text_format("<abc/>", "application/xml", limits)
    assert result.get("_quality", "failed") == "failed"
    assert "xml_input_capped" in cast(list[str], result["quality_flags"])


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
    """Binary container (PDF) below the quality floor: empty text, partial
    encoding flag, quality=low."""
    result = extract(b"%PDF-1.4 fake pdf content", "application/pdf")
    assert result.quality == Quality.LOW
    assert result.text == ""
    assert result.markdown == ""
    assert "pdf_encoding_partial" in result.flags


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


def test_xml_gt_in_quoted_attribute_extracts():
    content = b'<root><item id="a>b"/>Hello</root>'
    result = extract(content, "application/xml")
    assert result.quality == Quality.MEDIUM
    assert "Hello" in result.text


def test_xml_cdata_with_markup_extracts():
    content = b"<root><![CDATA[<b>unclosed]]></root>"
    result = extract(content, "application/xml")
    assert result.quality == Quality.MEDIUM
    assert "<b>unclosed" in result.text


def test_xml_equal_count_mismatched_nesting_refused():
    content = b"<a><b></a></b>"
    result = extract(content, "application/xml")
    assert result.quality == Quality.FAILED
    assert "xml_parse_error" in result.flags


def test_xml_valid_document_still_extracts():
    content = b'<?xml version="1.0"?><catalog><book><title>Hello</title></book></catalog>'
    result = extract(content, "application/xml")
    assert result.quality == Quality.MEDIUM
    assert "Hello" in result.text
    assert result.metadata["root"] == "catalog"


def test_xml_external_dtd_refused_before_parse():
    content = b'<!DOCTYPE root SYSTEM "http://example.invalid/evil.dtd"><root>ok</root>'
    result = extract(content, "application/xml")
    assert result.quality == Quality.FAILED
    assert "xml_dtd_refused" in result.flags
    assert result.text == ""
