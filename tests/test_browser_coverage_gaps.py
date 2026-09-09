"""TestBrowserCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestBrowserCoverageGaps:
    """Cover remaining branches in browser.py"""

    async def test_browser_renderer_reset_browser_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()
        await renderer._reset_browser()
        renderer._browser = None
        await renderer._reset_browser()

    async def test_browser_renderer_close_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()
        await renderer.close()
        await renderer.close()

    def test_browser_renderer_pdf_text_extractor_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()
        text = renderer._pdf_text_extractor("http://127.0.0.1:1/nonexistent.pdf")
        assert text == ""

    async def test_browser_renderer_capture_element_screenshots_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()

        class MockPage:
            async def evaluate(self, _):
                return []

        result = await renderer._capture_element_screenshots(MockPage(), [])
        assert result == []

    async def test_browser_renderer_flatten_shadow_dom_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()

        class MockPage:
            async def evaluate(self, _):
                raise Exception("flatten failed")

        await renderer._flatten_shadow_dom(MockPage())

    async def test_browser_renderer_extract_with_challenge_wait_timeout(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()
        renderer._challenge_wait_seconds = 0.001

        # Page that always shows Cloudflare challenge
        class MockPage:
            async def evaluate(self, script):
                return "Just a moment..."

            async def content(self):
                return "<html><body>Just a moment...</body></html>"

        html, text, classification, escaped = await renderer._extract_with_challenge_wait(
            MockPage(), type("obj", (), {"include_html": True, "include_text": True})()
        )
        assert classification == "bot_challenge_detected"
        assert not (escaped)

    async def test_browser_renderer_extract_with_challenge_wait_escaped(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()
        renderer._challenge_wait_seconds = 0.001
        call_count = [0]

        class MockPage:
            async def evaluate(self, script):
                call_count[0] += 1
                if call_count[0] <= 3:
                    return "Just a moment..."
                return "Normal page content"

            async def content(self):
                if call_count[0] <= 3:
                    return "<html><body>Just a moment...</body></html>"
                return "<html><body>Normal page content</body></html>"

        html, text, classification, escaped = await renderer._extract_with_challenge_wait(
            MockPage(), type("obj", (), {"include_html": True, "include_text": True})()
        )
        assert classification != "bot_challenge_detected"
        assert escaped

    async def test_browser_renderer_extract_html_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()

        class MockPage:
            async def content(self):
                raise Exception("content failed")

        result = await renderer._extract_html(MockPage())
        assert result == ""

    async def test_browser_renderer_extract_text_error(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()

        class MockPage:
            async def evaluate(self, _):
                raise Exception("evaluate failed")

        result = await renderer._extract_text(MockPage())
        assert result == ""

    async def test_browser_renderer_auto_accept_cookie_consent(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()

        class MockPage:
            async def query_selector(self, _):
                return None

        # Opt-in required (M4): disabled by default, no click without consent.
        result = await renderer._auto_accept_cookie_consent(MockPage(), False)
        assert not (result)

    async def test_browser_renderer_click_consent_button(self):
        from app.crawl.strategies.browser import BrowserRenderer

        renderer = BrowserRenderer()

        class MockElement:
            async def is_visible(self):
                return True

            async def click(self):
                pass

        class MockPage:
            def __init__(self):
                self.clicked = False

            async def query_selector(self, selector):
                if "accept" in selector:
                    return MockElement()
                return None

        result = await renderer._auto_accept_cookie_consent(MockPage(), True)
        assert result
