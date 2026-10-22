"""LLM output formatting: metadata extraction and markdown edges."""

from app.extraction.llm_format import extract_llm_metadata, text_from_html
from app.models.json_value import JsonValue
from app.models.page_metadata import PageMetadata
from app.models.page_metadata_response import PageMetadataResponse
from tests._extraction_helpers import extract_content
from tests._fetcher_factories import LLM_FORMAT_TEST_HTML
from tests._parse_budget import ParseBudget
from tests._policy_factories import canonical


class TestPageMetadata:
    def test_extracts_title_and_meta(self):
        meta = extract_llm_metadata(LLM_FORMAT_TEST_HTML, canonical("https://example.com"))
        assert meta["title"] == "Test Page"
        assert meta["description"] == "A test page"
        assert meta["og_title"] == "OG Title"
        assert meta["language"] == "en"
        assert meta["source_url"] == "https://example.com"

    def test_handles_empty(self):
        assert extract_llm_metadata("", canonical("https://example.com")) == {
            "source_url": "https://example.com"
        }


class TestJsonLdExtraction:
    def test_json_ld_populated_from_script_tag(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">{"@context":"https://schema.org","name":"Test"}</script>'
            "</head><body><p>Body</p></body></html>"
        )
        meta = extract_llm_metadata(html, canonical("https://example.com"))
        assert meta.get("json_ld") is not None
        assert meta["json_ld"]["name"] == "Test"
        assert meta["json_ld"]["@context"] == "https://schema.org"

    def test_json_ld_multiple_blocks_become_list(self):
        html = (
            '<script type="application/ld+json">{"@type":"A"}</script>'
            '<script type="application/ld+json">{"@type":"B"}</script>'
        )
        meta = extract_llm_metadata(html, canonical("https://example.com"))
        assert isinstance(meta["json_ld"], list)
        assert len(meta["json_ld"]) == 2

    def test_json_ld_absent_when_no_script(self):
        meta = extract_llm_metadata("<html><body>x</body></html>", canonical("https://example.com"))
        assert "json_ld" not in meta

    def test_a_scalar_json_ld_block_survives_to_the_wire(self):
        """A JSON-LD script is arbitrary JSON: a bare scalar is valid JSON, the
        extractor produces it, and the wire model must admit it."""
        html = '<html><head><script type="application/ld+json">42</script></head><body>x</body></html>'
        meta = extract_llm_metadata(html, canonical("https://example.com"))
        assert meta["json_ld"] == 42
        # The wire model admits what the extractor can produce.
        projected = PageMetadataResponse(
            **{k: meta[k] for k in PageMetadataResponse.model_fields & meta.keys()}
        )
        assert projected.json_ld == 42

    def test_page_metadata_declares_json_ld_as_the_system_type(self):
        """Both declarations of the passthrough payload name the system type, not a
        shape the arbitrary-JSON door never promised."""
        json_ld_annotation = PageMetadata.__annotations__["json_ld"]
        assert getattr(json_ld_annotation, "__forward_arg__", json_ld_annotation) == "JsonValue", (
            "the owner must name the system type"
        )
        assert PageMetadataResponse.model_fields["json_ld"].annotation == JsonValue | None

    def test_single_parse_per_html(self):
        budget = ParseBudget()
        html = (
            '<html lang="en"><head><title>Test Page</title>'
            '<meta name="description" content="A test page">'
            '<meta property="og:title" content="OG Title">'
            '<link rel="canonical" href="https://example.com/c">'
            '<link rel="icon" href="https://example.com/f.ico">'
            '<script type="application/ld+json">{"@type":"WebSite"}</script>'
            "</head><body><p>x</p></body></html>"
        )
        meta = extract_llm_metadata(html, canonical("https://example.com"))
        # Title, meta, JSON-LD, canonical, favicon, language all read from ONE lxml parse instead of 8+
        # independent regex scans.
        assert budget.parses == 1
        assert meta["title"] == "Test Page"
        assert meta["canonical_url"] == "https://example.com/c"
        assert meta["favicon"] == "https://example.com/f.ico"


class TestLlmFormat:
    def test_extract_llm_metadata_handles_empty(self):
        meta = extract_llm_metadata("", canonical("https://example.com"))
        assert meta == {"source_url": "https://example.com"}

    def test_extract_content_html_returns_markdown(self):
        html = "<html><body><main>Content here</main></body></html>"
        result = extract_content(html, "text/html", canonical("https://example.com"))
        assert "Content here" in (result.markdown or "")

    def test_text_from_html_strips_tags(self):
        html = "<html><body><script>var x=1;</script><style>body{}</style><p>Hello</p></body></html>"
        text = text_from_html(html)
        assert "Hello" in text
        assert "var x" not in text
        assert "body{}" not in text


class TestLlmFormatEdges:
    def test_malformed_json_ld_is_ignored(self):
        md = extract_llm_metadata(
            '<script type="application/ld+json">{not valid json</script>',
            canonical("http://x/"),
        )
        assert "json_ld" not in md

    def test_canonical_and_favicon(self):
        html = '<link rel="canonical" href="https://canon.example/"><link rel="icon" href="/fav.ico">'
        md = extract_llm_metadata(html, canonical("http://x/"))
        assert md["canonical_url"] == "https://canon.example/"
        assert md["favicon"] == "/fav.ico"
