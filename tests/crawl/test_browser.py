"""Real headless-browser behavior via Camoufox (no mocks)."""

import base64

import pytest

from app.models.crawl_result import CrawlResult
from app.models.render_options import RenderOptions
from app.orchestration.engine import crawl_request
from app.safety.validation import HostPolicy
from app.transport.browser import BrowserRenderer
from app.transport.cache import HttpCache
from app.vocabulary.strategies import BROWSER_HEADLESS
from tests._assertions import assert_result_success

pytestmark = pytest.mark.browser


class TestBrowserRenderer:
    @pytest.fixture(autouse=True)
    async def setup_renderer(self):
        self.renderer = BrowserRenderer()
        # Reuse a single browser instance across all tests
        yield
        await self.renderer.close()

    async def _render(self, path: str, base_url: str, **options) -> CrawlResult:
        url = f"{base_url}{path}"
        opts = RenderOptions(browser_profile=options.pop("browser_profile", {}), **options)
        return await self.renderer.render(
            url,
            opts,
            policy=HostPolicy.unrestricted(),
        )

    async def test_browser_renders_html_content(self, base_url):
        result = await self._render("/", base_url)
        assert_result_success(result)
        assert "Example Org" in (result.text or "")
        assert "Example Org" in (result.html or "")

    async def test_browser_captures_xhr_and_fetch_endpoints(self, base_url):
        result = await self._render("/dynamic", base_url)
        intercepted = result.metadata.get("intercepted_xhr") or []
        urls = [entry["url"] for entry in intercepted]
        assert any(url.endswith("/api/data") for url in urls), intercepted
        # The page appends the fetched JSON to the DOM, so the data appears in text.
        assert '"items"' in (result.text or "")

    async def test_browser_lazy_scroll_triggers_deferred_content(self, base_url):
        result = await self._render("/lazy", base_url)
        # The lazy div is only revealed after scrolling to the bottom.
        assert "Lazy Content Revealed" in (result.html or "")

    async def test_browser_captures_screenshot_as_base64(self, base_url):
        result = await self._render("/", base_url, browser_profile={"include_screenshot": True})
        screenshot = result.metadata.get("screenshot")
        assert isinstance(screenshot, str)
        assert len(screenshot) > 0
        decoded = base64.b64decode(screenshot)
        assert decoded.startswith(b"\x89PNG") or len(decoded) > 0

    async def test_browser_classifies_access_denied_page(self, base_url):
        result = await self._render("/blocked", base_url)
        assert result.classification == "access_denied"
        assert result.status == "blocked"

    async def test_crawl_request_uses_browser_strategy(self, base_url):
        payload = {
            "url": f"{base_url}/",
            "policy": {
                "strategies": [BROWSER_HEADLESS],
                "limits": {"timeout_seconds": 20, "max_retries": 1, "max_discovered_links": 50},
            },
        }

        async def browser_worker(job):
            return await self.renderer.render(job.url.raw, job.options, policy=job.host_policy)

        response = await crawl_request(
            payload,
            browser_worker=browser_worker,
            cache=HttpCache(),
        )
        assert response["submitted_count"] == 1
        assert "Example Org" in response["results"][0]["markdown"]
