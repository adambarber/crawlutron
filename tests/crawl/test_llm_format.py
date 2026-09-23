"""LLM output formatting: metadata extraction and markdown edges."""

from _testkit import *
from _testkit import LLM_FORMAT_TEST_HTML

from app.crawl.llm_format import _extract_llm_metadata, _text_from_html


class TestPageMetadata:
    def test_extracts_title_and_meta(self):
        meta = _extract_llm_metadata(LLM_FORMAT_TEST_HTML, "https://example.com")
        assert meta["title"] == "Test Page"
        assert meta["description"] == "A test page"
        assert meta["og_title"] == "OG Title"
        assert meta["language"] == "en"
        assert meta["source_url"] == "https://example.com"

    def test_handles_empty(self):
        assert _extract_llm_metadata("", "https://example.com") == {"source_url": "https://example.com"}

class TestJsonLdExtraction:
    def test_json_ld_populated_from_script_tag(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">{"@context":"https://schema.org","name":"Test"}</script>'
            "</head><body><p>Body</p></body></html>"
        )
        meta = _extract_llm_metadata(html, "https://example.com")
        assert meta.get("json_ld") is not None
        assert meta["json_ld"]["name"] == "Test"
        assert meta["json_ld"]["@context"] == "https://schema.org"

    def test_json_ld_multiple_blocks_become_list(self):
        html = (
            '<script type="application/ld+json">{"@type":"A"}</script>'
            '<script type="application/ld+json">{"@type":"B"}</script>'
        )
        meta = _extract_llm_metadata(html, "https://example.com")
        assert isinstance(meta["json_ld"], list)
        assert len(meta["json_ld"]) == 2

    def test_json_ld_absent_when_no_script(self):
        meta = _extract_llm_metadata("<html><body>x</body></html>", "https://example.com")
        assert "json_ld" not in meta

    def test_single_parse_per_html(self, monkeypatch):
        import app.crawl.extraction as extraction

        calls = {"n": 0}
        real = extraction._parse_html_tree

        def spy(h):
            calls["n"] += 1
            return real(h)

        monkeypatch.setattr(extraction, "_parse_html_tree", spy)
        html = (
            '<html lang="en"><head><title>Test Page</title>'
            '<meta name="description" content="A test page">'
            '<meta property="og:title" content="OG Title">'
            '<link rel="canonical" href="https://example.com/c">'
            '<link rel="icon" href="https://example.com/f.ico">'
            '<script type="application/ld+json">{"@type":"WebSite"}</script>'
            "</head><body><p>x</p></body></html>"
        )
        meta = _extract_llm_metadata(html, "https://example.com")
        # #15: title, meta, JSON-LD, canonical, favicon, language all read from
        # ONE lxml parse instead of 8+ independent regex scans.
        assert calls["n"] == 1
        assert meta["title"] == "Test Page"
        assert meta["canonical_url"] == "https://example.com/c"
        assert meta["favicon"] == "https://example.com/f.ico"


class TestLlmFormat:
    def test_extract_llm_metadata_handles_empty(self):
        meta = _extract_llm_metadata("", "https://example.com")
        assert meta == {"source_url": "https://example.com"}

    def test_extract_content_html_returns_markdown(self):
        html = "<html><body><main>Content here</main></body></html>"
        result = extract_content(html, "text/html", "https://example.com")
        assert "Content here" in (result.markdown or "")

    def test_text_from_html_strips_tags(self):
        html = "<html><body><script>var x=1;</script><style>body{}</style><p>Hello</p></body></html>"
        text = _text_from_html(html)
        assert "Hello" in text
        assert "var x" not in text
        assert "body{}" not in text


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
