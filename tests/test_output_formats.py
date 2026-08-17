"""#17 Multiple output formats - pure serializer tests (no mocks)."""

import json

from app.output_formats import (
    SUPPORTED_OUTPUT_FORMATS,
    serialize_results,
)



SAMPLE = [
    {
        "original_url": "http://a.example/",
        "final_url": "http://a.example/",
        "status": "success",
        "classification": "rendered",
        "selected_strategy": "HTTP_DECLARED_CRAWLER",
        "quality_score": 1.0,
        "error": None,
        "http_status": 200,
        "discovered_link_count": 2,
        "markdown": "Hello A",
        "metadata": {"title": "A"},
    },
    {
        "original_url": "http://b.example/",
        "final_url": "http://b.example/",
        "status": "failed",
        "classification": "not_found",
        "selected_strategy": None,
        "quality_score": 0.0,
        "error": "not_found",
        "http_status": 404,
        "discovered_link_count": 0,
        "markdown": None,
        "metadata": {},
    },
]



def test_supported_formats_constant():
    assert set(SUPPORTED_OUTPUT_FORMATS) == {"json", "jsonl", "ndjson", "markdown", "csv"}



def test_json_default_is_pretty_array():
    body, media = serialize_results(SAMPLE, "json")
    assert media == "application/json"
    parsed = json.loads(body)
    assert isinstance(parsed, list) and len(parsed) == 2
    # pretty (indented) JSON spans multiple lines
    assert body.count(chr(10)) > 2



def test_jsonl_one_object_per_line():
    body, media = serialize_results(SAMPLE, "jsonl")
    assert media == "application/x-ndjson"
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["original_url"] == "http://a.example/"
    assert json.loads(lines[1])["original_url"] == "http://b.example/"



def test_ndjson_equals_jsonl_media_and_shape():
    body_j, media_j = serialize_results(SAMPLE, "jsonl")
    body_n, media_n = serialize_results(SAMPLE, "ndjson")
    assert media_n == "application/x-ndjson"
    assert body_n == body_j



def test_markdown_has_one_section_per_url_and_content():
    body, media = serialize_results(SAMPLE, "markdown")
    assert media == "text/markdown; charset=utf-8"
    assert body.count("# http://") == 2
    assert "Hello A" in body
    assert "not_found" in body



def test_csv_has_header_and_one_row_per_result():
    body, media = serialize_results(SAMPLE, "csv")
    assert media == "text/csv; charset=utf-8"
    rows = body.splitlines()
    assert rows[0] == "original_url,normalized_url,final_url,status,classification,selected_strategy,quality_score,discovered_link_count,error,http_status"
    assert len(rows) == 3  # header + 2 rows
    assert rows[1].startswith("http://a.example/")
    assert "success" in rows[1]



def test_none_format_defaults_to_json():
    body, media = serialize_results(SAMPLE, None)
    assert media == "application/json"
    assert json.loads(body)[0]["original_url"] == "http://a.example/"



def test_unsupported_format_raises():
    try:
        serialize_results(SAMPLE, "xml")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert str(exc) == "unsupported_output_format: xml"
