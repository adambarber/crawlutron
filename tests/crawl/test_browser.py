"""Real headless-browser behavior via Camoufox (no mocks)."""

import asyncio
import base64
import unittest

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.crawl.strategies.browser import BrowserRenderer
from app.models import RenderOptions
from helpers import ServerTestCase
from app.constants import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER


class BrowserRendererTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.renderer = BrowserRenderer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.renderer.close()
        super().tearDownClass()

    def _render(self, path: str, **options) -> object:
        url = f"{self.base}{path}"
        opts = RenderOptions(browser_profile=options.pop("browser_profile", {}), **options)
        return asyncio.run(self.renderer.render(url, opts))

    def test_browser_renders_html_content(self) -> None:
        result = self._render("/")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.classification, "rendered")
        self.assertIn("Example Org", result.text or "")
        self.assertIn("Example Org", result.html or "")

    def test_browser_captures_xhr_and_fetch_endpoints(self) -> None:
        result = self._render("/dynamic")
        intercepted = result.metadata.get("intercepted_xhr") or []
        urls = [entry["url"] for entry in intercepted]
        self.assertTrue(any(url.endswith("/api/data") for url in urls), intercepted)
        # The page appends the fetched JSON to the DOM, so the data appears in text.
        self.assertIn('"items"', result.text or "")

    def test_browser_lazy_scroll_triggers_deferred_content(self) -> None:
        result = self._render("/lazy")
        # The lazy div is only revealed after scrolling to the bottom.
        self.assertIn("Lazy Content Revealed", result.html or "")

    def test_browser_captures_screenshot_as_base64(self) -> None:
        result = self._render("/", browser_profile={"include_screenshot": True})
        screenshot = result.metadata.get("screenshot")
        self.assertIsInstance(screenshot, str)
        self.assertTrue(len(screenshot) > 0)
        decoded = base64.b64decode(screenshot)
        self.assertTrue(decoded.startswith(b"\x89PNG") or len(decoded) > 0)

    def test_browser_classifies_access_denied_page(self) -> None:
        result = self._render("/blocked")
        self.assertEqual(result.classification, "access_denied")
        self.assertEqual(result.status, "blocked")

    def test_crawl_request_uses_browser_strategy(self) -> None:
        payload = self.make_payload(
            strategies=[BROWSER_HEADLESS],
            limits={"timeout_seconds": 20, "max_retries": 1, "max_discovered_links": 50},
        )
        response = asyncio.run(
            crawl_request(
                payload,
                browser_worker=lambda job: self.renderer.render(job.url, job.options),
                cache=HttpCache(),
            )
        )
        self.assertEqual(response["submitted_count"], 1)
        self.assertIn("Example Org", response["results"][0]["markdown"])


if __name__ == "__main__":
    unittest.main()
