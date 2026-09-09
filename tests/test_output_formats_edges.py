"""Output format edge cases: CSV injection guards, unknown formats, defaults."""

from coverage_helpers import *


class TestOutputFormatsEdges:
    """Cover remaining branches in output_formats.py"""

    def test_serialize_default_fallback(self):
        from app.output_formats import _default

        class CustomObj:
            def __init__(self):
                self.attr = "value"

        obj = CustomObj()
        result = _default(obj)
        assert result == {"attr": "value"}

    def test_serialize_results_json(self):
        from app.output_formats import serialize_results

        results = [{"url": "http://x/", "text": "content"}]
        output, media_type = serialize_results(results, "json")
        assert "http://x/" in output
        assert media_type == "application/json"

    def test_serialize_results_jsonl(self):
        from app.output_formats import serialize_results

        results = [{"url": "http://x/", "text": "content"}]
        output, media_type = serialize_results(results, "jsonl")
        assert "http://x/" in output
        assert media_type == "application/x-ndjson"

    def test_serialize_results_ndjson(self):
        from app.output_formats import serialize_results

        results = [{"url": "http://x/", "text": "content"}]
        output, media_type = serialize_results(results, "ndjson")
        assert "http://x/" in output
        assert media_type == "application/x-ndjson"

    def test_serialize_results_markdown(self):
        from app.output_formats import serialize_results

        results = [{"url": "http://x/", "markdown": "# Title\n\nContent"}]
        output, media_type = serialize_results(results, "markdown")
        assert "# Title" in output
        assert media_type == "text/markdown; charset=utf-8"

    def test_serialize_results_csv(self):
        from app.output_formats import serialize_results

        results = [
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
        from app.output_formats import serialize_results

        results = [{"url": "http://x/", "text": "content"}]
        with pytest.raises(ValueError):
            serialize_results(results, "unknown")
