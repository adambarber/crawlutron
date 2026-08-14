"""Content extraction pipeline + Katana-inspired discovery (js_endpoints, pagination)."""

import unittest

from app.crawl.extraction import (
    _detect_pagination,
    extract_content,
    ExtractedContent,
    _is_html,
    _looks_like_document,
    _text_from_html,
    _extract_html,
    extract_js_endpoints,
)
from app.crawl.firecrawl_format import _extract_firecrawl_metadata


class TestExtractContent(unittest.TestCase):
    def test_html_uses_trafilatura_markdown(self) -> None:
        html = "<html><body><main>Main content here</main></body></html>"
        result = extract_content(html, "text/html", "http://example.com/")
        self.assertIsInstance(result, ExtractedContent)
        self.assertIn("Main content here", (result.markdown or ""))
        self.assertEqual(result.text, result.markdown)

    def test_pdf_routes_to_markitdown_fallback(self) -> None:
        result = extract_content(b"%PDF-1.7 fake bytes", "application/pdf", "http://x/a.pdf")
        self.assertEqual(result.text, "%PDF-1.7 fake bytes")

    def test_csv_routes_to_markitdown_table(self) -> None:
        body = b"name,age\nAlice,30\nBob,25"
        result = extract_content(body, "text/csv", "http://x/data.csv")
        self.assertIn("Alice", (result.text or ""))
        self.assertIn("|", (result.markdown or ""))

    def test_json_passes_through_markitdown(self) -> None:
        body = b'{"a":1,"b":[2,3]}'
        result = extract_content(body, "application/json", "http://x/d.json")
        self.assertIn('"a":1', (result.text or ""))

    def test_plain_text_passes_through(self) -> None:
        result = extract_content("just some plain text", "text/plain", "http://x/note.txt")
        self.assertEqual(result.text, "just some plain text")
        self.assertEqual(result.markdown, "just some plain text")

    def test_doctype_without_content_type_routes_to_html(self) -> None:
        html = "<!doctype html><html><body>x</body></html>"
        result = extract_content(html, None, "http://x/")
        self.assertIn("x", (result.markdown or ""))

    def test_unrecognized_binary_passes_through_as_text(self) -> None:
        result = extract_content(b"\x00\x01binary", "application/octet-stream", "http://x/bin")
        self.assertIsNotNone(result.text)

    def test_empty_or_unparseable_document_yields_no_content(self) -> None:
        result = extract_content(b"", "application/pdf", "http://x/e.pdf")
        self.assertIsNone(result.text)
        self.assertIsNone(result.markdown)


class TestRoutingHelpers(unittest.TestCase):
    def test_is_html(self) -> None:
        self.assertTrue(_is_html("text/html", "<html></html>"))
        self.assertTrue(_is_html("text/html; charset=utf-8", b"<html></html>"))
        self.assertFalse(_is_html("application/json", '{"a":1}'))
        self.assertTrue(_is_html(None, "<!doctype html><html></html>"))
        self.assertFalse(_is_html(None, "plain"))

    def test_looks_like_document(self) -> None:
        self.assertTrue(_looks_like_document("application/pdf", b"%PDF", "http://x/a.pdf"))
        self.assertTrue(_looks_like_document("application/octet-stream", b"%PDF-1.4 data", "http://x/mystery"))
        self.assertTrue(_looks_like_document("text/csv", b"a,b", "http://x/d.csv"))
        self.assertTrue(_looks_like_document("application/json", b"{}", None))
        self.assertFalse(_looks_like_document("text/html", b"<html>", "http://x/"))
        self.assertFalse(_looks_like_document("text/plain", b"hi", "http://x/note.txt"))


class TestTextAndMetadata(unittest.TestCase):
    def test_text_from_html_strips_scripts_and_tags(self) -> None:
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        self.assertEqual(_text_from_html(html), "Hello World")


class TestKatanaDiscovery(unittest.TestCase):
    def test_extract_js_endpoints_finds_fetch_call(self) -> None:
        html = '<script>fetch("/api/data").then(r => r.json())</script>'
        endpoints = extract_js_endpoints(html)
        self.assertIn({"url": "/api/data"}, endpoints)

    def test_extract_js_endpoints_finds_axios_and_xhr(self) -> None:
        html = (
            'axios.get("/api/items");'
            'xhr.open("GET", "/api/widgets");'
        )
        endpoints = extract_js_endpoints(html)
        self.assertIn({"url": "/api/items"}, endpoints)
        self.assertIn({"url": "/api/widgets"}, endpoints)

    def test_extract_js_endpoints_dedupes(self) -> None:
        html = 'fetch("/api/x"); fetch("/api/x")'
        endpoints = extract_js_endpoints(html)
        self.assertEqual(endpoints, [{"url": "/api/x"}])

    def test_extract_js_endpoints_resolves_against_source(self) -> None:
        html = 'fetch("/api/x"); axios.get("/y")'
        endpoints = extract_js_endpoints(html, "http://e.com/page")
        self.assertEqual(
            endpoints,
            [{"url": "http://e.com/api/x"}, {"url": "http://e.com/y"}],
        )

    def test_extract_js_endpoints_dedupes_resolved(self) -> None:
        html = 'fetch("/api/x"); fetch("/api/x")'
        endpoints = extract_js_endpoints(html, "http://e.com/")
        self.assertEqual(endpoints, [{"url": "http://e.com/api/x"}])

    def test_extract_js_endpoints_unresolved_without_source(self) -> None:
        # Without a source URL the raw endpoint string is returned unresolved.
        html = 'fetch("/api/x")'
        endpoints = extract_js_endpoints(html)
        self.assertEqual(endpoints, [{"url": "/api/x"}])

    def test_extract_js_endpoints_empty_html(self) -> None:
        self.assertEqual(extract_js_endpoints(""), [])
        self.assertEqual(extract_js_endpoints(None), [])  # type: ignore[arg-type]

    def test_extract_js_endpoints_skips_empty_endpoint(self) -> None:
        # A regex match with an empty/whitespace endpoint is skipped.
        endpoints = extract_js_endpoints('<script>fetch(" ")</script>', "http://e.com/")
        self.assertEqual(endpoints, [])

    def test_extract_html_markdownify_fallback(self) -> None:
        # Trafilatura rejects an empty body; the markdownify fallback runs
        # (and may still yield nothing, falling through to plain-text).
        result = _extract_html("<html><body></body></html>", None)
        self.assertIsInstance(result, ExtractedContent)
        self.assertTrue(result.markdown is None or isinstance(result.markdown, str))

    def test_detect_pagination_anchor_rel_next(self) -> None:
        html = '<a rel="next" href="/page2">Next</a>'
        self.assertEqual(_detect_pagination(html, "https://example.com/list"), "https://example.com/page2")

    def test_detect_pagination_link_rel_next(self) -> None:
        html = '<link rel="next" href="/p3">'
        self.assertEqual(_detect_pagination(html, "https://example.com/"), "https://example.com/p3")

    def test_detect_pagination_none_without_hint(self) -> None:
        html = '<a href="/page2">Next</a>'
        self.assertIsNone(_detect_pagination(html, "https://example.com/"))


if __name__ == "__main__":
    unittest.main()
