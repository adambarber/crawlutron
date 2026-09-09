"""TestLlmFormatEdges coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestLlmFormatEdges:
    def test_malformed_json_ld_is_ignored(self):
        md = _extract_llm_metadata(
            '<script type="application/ld+json">{not valid json</script>',
            "http://x/",
        )
        assert "json_ld" not in md

    def test_canonical_and_favicon(self):
        html = '<link rel="canonical" href="https://canon.example/"><link rel="icon" href="/fav.ico">'
        md = _extract_llm_metadata(html, "http://x/")
        assert md["canonical_url"] == "https://canon.example/"
        assert md["favicon"] == "/fav.ico"
