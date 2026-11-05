"""Output formats: field projection, CSV injection neutralization and envelope edges."""

import json

import pytest

from crawlutron.extraction.output_formats import (
    SUPPORTED_OUTPUT_FORMATS,
    csv_neutralize,
    serialize_results,
)
from crawlutron.models.json_value import JsonValue

# The two result shapes the serialization table is built from. Module level so the table reads as
# formats-over-bodies rather than as four bodies spelling the same result four times.
_TEXT_RESULT: list[dict[str, JsonValue]] = [{"url": "http://x/", "text": "content"}]
_MARKDOWN_RESULT: list[dict[str, JsonValue]] = [{"url": "http://x/", "markdown": "# Title\n\nContent"}]

SAMPLE: list[dict[str, JsonValue]] = [
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
    body_j, _ = serialize_results(SAMPLE, "jsonl")
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
    assert (
        rows[0] == "original_url,normalized_url,final_url,status,classification,selected_strategy,"
        "quality_score,discovered_link_count,error,http_status"
    )
    assert len(rows) == 3  # header + 2 rows
    assert rows[1].startswith("http://a.example/")
    assert "success" in rows[1]


def test_none_format_defaults_to_json():
    body, media = serialize_results(SAMPLE, None)
    assert media == "application/json"
    assert json.loads(body)[0]["original_url"] == "http://a.example/"


def test_unsupported_format_raises():
    with pytest.raises(ValueError, match="^unsupported_output_format: xml$"):
        serialize_results(SAMPLE, "xml")


class TestOutputFormatDefaults:
    def test_output_formats_default_with_dict(self):
        from crawlutron.extraction.output_formats import jsonify

        # Object with model_dump method
        class FakeObj:
            def model_dump(self):
                return {"serialized": "true"}

        result = jsonify(FakeObj())
        assert result == {"serialized": "true"}

    def test_output_formats_default_dict_method(self):
        from crawlutron.extraction.output_formats import jsonify

        class DictObj:
            def dict(self):
                return {"from_dict": "value"}

        result = jsonify(DictObj())
        assert result == {"from_dict": "value"}

    def test_output_formats_default_str_fallback(self):
        from crawlutron.extraction.output_formats import jsonify

        result = jsonify(42)
        assert result == "42"


class TestOutputFormatSerialization:
    def test_output_formats_serialize_unknown_format(self):
        from crawlutron.extraction.output_formats import serialize_results

        results: list[dict[str, JsonValue]] = [{"original_url": "http://x/"}]
        with pytest.raises(ValueError):
            serialize_results(results, "unknown_format")


class TestOutputFormatSafeCoercion:
    def test_output_formats_safe_json(self):
        from crawlutron.extraction.output_formats import safe_value

        val = {"nested": [1, 2, 3]}
        result = safe_value(val)
        assert isinstance(result, str)

    def test_output_formats_safe_none(self):
        from crawlutron.extraction.output_formats import safe_value

        result = safe_value(None)
        assert result == ""

    def test_output_formats_safe_string(self):
        from crawlutron.extraction.output_formats import safe_value

        result = safe_value("hello")
        assert result == "hello"

    def test_output_formats_safe_int(self):
        from crawlutron.extraction.output_formats import safe_value

        result = safe_value(42)
        assert result == "42"


def test_csv_neutralize_quotes_equals_prefix():
    """= prefix is the most common formula trigger; must be quoted."""
    assert csv_neutralize("=cmd|'/c calc'!A1", "error") == "'=cmd|'/c calc'!A1"


def test_csv_neutralize_handles_all_dangerous_prefixes():
    for prefix in ("+", "-", "@"):
        value = f"{prefix}payload"
        assert csv_neutralize(value, "error") == f"'{value}"
    # TAB and CR are also dangerous; the literal-escape strings we pass.
    for prefix in ("\t", "\r"):
        value = f"{prefix}payload"
        assert csv_neutralize(value, "error") == f"'{value}"


def test_csv_neutralize_passes_safe_values():
    """Plain text and non-prefixed values must be returned unchanged."""
    assert csv_neutralize("plain text", "error") == "plain text"
    assert csv_neutralize("404 not found", "error") == "404 not found"
    assert csv_neutralize("see https://x/?q=test", "error") == "see https://x/?q=test"


def test_csv_neutralize_skips_non_neutralized_columns():
    """URL columns are returned unchanged so existing analyses still work."""
    assert csv_neutralize("=foo", "original_url") == "=foo"
    assert csv_neutralize("=foo", "normalized_url") == "=foo"
    assert csv_neutralize("=foo", "final_url") == "=foo"


def test_csv_neutralize_handles_empty_value():
    assert csv_neutralize("", "error") == ""


def test_serialize_results_csv_neutralizes_attacker_content():
    """End-to-end: an error field that starts with = is quoted in the CSV."""
    results: list[dict[str, JsonValue]] = [{"original_url": "http://x/", "error": "=cmd|'/c calc'!A1"}]
    body, media_type = serialize_results(results, "csv")
    assert media_type == "text/csv; charset=utf-8"
    # The formula trigger must be preceded by a single quote.
    assert ",'=cmd" in body
    # The raw unescaped formula must NOT appear (it would mean the attacker payload was emitted unquoted) in
    # the error column where the payload lands (column index 8, per _CSV_COLUMNS).
    error_cell = body.splitlines()[1].split(",")[8]
    assert error_cell.startswith("'"), error_cell


def test_serialize_results_csv_preserves_url_columns():
    """URL columns are NOT neutralized: =foo in a URL is a query separator."""
    results: list[dict[str, JsonValue]] = [{"original_url": "https://x/?q=foo&p=bar", "error": "ok"}]
    body, _ = serialize_results(results, "csv")
    assert "https://x/?q=foo&p=bar" in body


class TestOutputFormatsEdges:
    """Cover remaining branches in output_formats.py"""

    def test_serialize_default_fallback(self):
        from crawlutron.extraction.output_formats import jsonify

        class CustomObj:
            def __init__(self):
                self.attr = "value"

        obj = CustomObj()
        result = jsonify(obj)
        assert result == {"attr": "value"}

    # One body per shape, named once.
    @pytest.mark.parametrize(
        ("fmt", "results", "needle", "media_type"),
        [
            ("json", _TEXT_RESULT, "http://x/", "application/json"),
            ("jsonl", _TEXT_RESULT, "http://x/", "application/x-ndjson"),
            ("ndjson", _TEXT_RESULT, "http://x/", "application/x-ndjson"),
            ("markdown", _MARKDOWN_RESULT, "# Title", "text/markdown; charset=utf-8"),
        ],
    )
    def test_serialize_results_carries_the_url_and_the_media_type(self, fmt, results, needle, media_type):
        """One row per text output format: the three line-oriented formats differ only in
        the name and media type the caller passes -- data, not behaviour -- so one table
        is one place a format rename has to land.
        """
        from crawlutron.extraction.output_formats import serialize_results

        output, out_media_type = serialize_results(results, fmt)
        assert needle in output
        assert out_media_type == media_type

    def test_serialize_results_csv(self):
        from crawlutron.extraction.output_formats import serialize_results

        results: list[dict[str, JsonValue]] = [
            {
                "original_url": "http://x/",
                "normalized_url": "http://x/",
                "final_url": "http://x/",
                "status": "success",
                "classification": "rendered",
                "selected_strategy": "HTTP_DECLARED_CRAWLER",
                "quality_score": 1.0,
                "discovered_link_count": 0,
                "error": None,
                "http_status": 200,
            }
        ]
        output, media_type = serialize_results(results, "csv")
        assert "http://x/" in output
        assert media_type == "text/csv; charset=utf-8"

    def test_serialize_results_unknown_format(self):
        from crawlutron.extraction.output_formats import serialize_results

        results: list[dict[str, JsonValue]] = [{"url": "http://x/", "text": "content"}]
        with pytest.raises(ValueError):
            serialize_results(results, "unknown")
