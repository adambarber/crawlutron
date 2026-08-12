"""Tests for browser.py module."""

import unittest
from app.browser import (
    BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    BrowserRenderer,
)

class BrowserRendererStatusTest(unittest.TestCase):
    def test_status_for_classification_rendered(self) -> None:
        renderer = BrowserRenderer()
        self.assertEqual(renderer._status_for_classification("rendered"), "success")

    def test_status_for_classification_blocked(self) -> None:
        renderer = BrowserRenderer()
        for classification in BLOCKED_CLASSIFICATIONS:
            with self.subTest(classification=classification):
                self.assertEqual(renderer._status_for_classification(classification), "blocked")

    def test_status_for_classification_failed(self) -> None:
        renderer = BrowserRenderer()
        for classification in FAILED_CLASSIFICATIONS:
            with self.subTest(classification=classification):
                self.assertEqual(renderer._status_for_classification(classification), "failed")

    def test_status_for_classification_unknown_defaults_to_blocked(self) -> None:
        renderer = BrowserRenderer()
        self.assertEqual(renderer._status_for_classification("unknown_classification"), "blocked")

    def test_looks_like_pdf(self) -> None:
        renderer = BrowserRenderer()
        self.assertTrue(renderer._looks_like_pdf("https://example.com/document.pdf"))
        self.assertTrue(renderer._looks_like_pdf("https://example.com/document.PDF"))
        self.assertTrue(renderer._looks_like_pdf("https://example.com/path/file.pdf?query=1"))
        self.assertFalse(renderer._looks_like_pdf("https://example.com/document.html"))
        self.assertFalse(renderer._looks_like_pdf("https://example.com/"))

    def test_headless_enabled_default(self) -> None:
        renderer = BrowserRenderer()
        self.assertTrue(renderer._headless_enabled())

    def test_headless_enabled_false_values(self) -> None:
        import os
        for val in ["0", "false", "no", "off", "FALSE"]:
            with self.subTest(val=val):
                os.environ["BROWSER_CRAWLER_HEADLESS"] = val
                try:
                    renderer = BrowserRenderer()
                    self.assertFalse(renderer._headless_enabled())
                finally:
                    del os.environ["BROWSER_CRAWLER_HEADLESS"]

if __name__ == "__main__":
    unittest.main()
