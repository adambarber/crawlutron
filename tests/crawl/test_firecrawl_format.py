"""Firecrawl-format metadata extraction, including JSON-LD (SOTA)."""

import unittest

from app.crawl.firecrawl_format import _extract_firecrawl_metadata, _text_from_html
from helpers import FIRECRAWL_TEST_HTML


class FirecrawlMetadataTest(unittest.TestCase):
    def test_extracts_title_and_meta(self) -> None:
        html = FIRECRAWL_TEST_HTML
        meta = _extract_firecrawl_metadata(html, "https://example.com")
        self.assertEqual(meta["title"], "Test Page")
        self.assertEqual(meta["description"], "A test page")
        self.assertEqual(meta["og_title"], "OG Title")
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["source_url"], "https://example.com")

    def test_handles_empty(self) -> None:
        self.assertEqual(_extract_firecrawl_metadata("", "https://example.com"), {"source_url": "https://example.com"})

    def test_text_from_html_strips_scripts_and_tags(self) -> None:
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        self.assertEqual(_text_from_html(html), "Hello World")


class JsonLdExtractionTest(unittest.TestCase):
    def test_json_ld_populated_from_script_tag(self) -> None:
        html = (
            "<html><head>"
            '<script type="application/ld+json">{"@context":"https://schema.org","name":"Test"}</script>'
            "</head><body><p>Body</p></body></html>"
        )
        meta = _extract_firecrawl_metadata(html, "https://example.com")
        self.assertIsNotNone(meta.get("json_ld"))
        self.assertEqual(meta["json_ld"]["name"], "Test")
        self.assertEqual(meta["json_ld"]["@context"], "https://schema.org")

    def test_json_ld_multiple_blocks_become_list(self) -> None:
        html = (
            '<script type="application/ld+json">{"@type":"A"}</script>'
            '<script type="application/ld+json">{"@type":"B"}</script>'
        )
        meta = _extract_firecrawl_metadata(html, "https://example.com")
        self.assertIsInstance(meta["json_ld"], list)
        self.assertEqual(len(meta["json_ld"]), 2)

    def test_json_ld_absent_when_no_script(self) -> None:
        meta = _extract_firecrawl_metadata("<html><body>x</body></html>", "https://example.com")
        self.assertNotIn("json_ld", meta)


if __name__ == "__main__":
    unittest.main()
