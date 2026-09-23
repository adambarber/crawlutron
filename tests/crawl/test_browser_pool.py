"""Browser pool: worker acquisition, timeout, sizing and global worker edges."""

import inspect

import pytest
from _testkit import *

import app.crawl.strategies.browser_pool as bp
from app import resource_pressure as rp
from app.crawl.strategies.browser_pool import (
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS,
    BrowserPool,
    BrowserPoolExhaustedError,
    close_browser_pool,
    get_browser_pool,
    set_browser_pool,
)


class TestAcquireTimeout:
    async def test_acquire_times_out_with_diagnostics(self):
        pool = BrowserPool(pool_size=1)
        await pool.initialize()
        held = await pool.acquire()
        try:
            try:
                await pool.acquire(timeout=0.05)
            except BrowserPoolExhaustedError as error:
                message = str(error)
            else:
                raise AssertionError("second acquire should have timed out")
            assert "pool_size=1" in message
            assert "available=0" in message
        finally:
            pool.release(held)
        # After release the pool serves again.
        renderer = await pool.acquire(timeout=1.0)
        pool.release(renderer)

    def test_acquire_default_timeout_is_bounded(self):
        params = inspect.signature(BrowserPool.acquire).parameters
        default = params["timeout"].default
        assert default == BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS
        assert default is not None and default > 0


class _StubPool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _clear():
    set_browser_pool(None)


def test_get_browser_pool_returns_none_before_initialize():
    # Contract: calling getters before startup must NOT silently construct a
    # BrowserPool with settings-unaware default size. It must be None.
    _clear()
    assert get_browser_pool() is None
    _clear()


def test_get_browser_pool_returns_set_pool():
    stub = _StubPool()
    try:
        set_browser_pool(stub)
        assert get_browser_pool() is stub
    finally:
        _clear()


@pytest.mark.asyncio
async def test_close_browser_pool_nulls_global():
    stub = _StubPool()
    try:
        set_browser_pool(stub)
        await close_browser_pool()
        assert stub.closed
        assert get_browser_pool() is None
    finally:
        _clear()


class _Mem:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes
        self.used_bytes = 0


@pytest.fixture(autouse=True)
def _clear_pool_env(monkeypatch):
    # Ensure no explicit override leaks between tests.
    monkeypatch.delenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", raising=False)
    yield


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", "3")
    assert bp._derive_browser_pool_size() == 3


def test_explicit_override_rejects_garbage(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", "not-a-number")
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    # garbled override -> falls through to budget derivation (3GiB -> 2)
    assert bp._derive_browser_pool_size() == 2


@pytest.mark.parametrize("raw", ["0", "-5", "  "])
def test_nonpositive_or_blank_override_falls_through(monkeypatch, raw):
    # An override that cannot mean a pool size (zero, negative, blank) is
    # garbage, not a clamp-to-1 request: derivation owns the sizing.
    monkeypatch.setenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", raw)
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    assert bp._derive_browser_pool_size() == 2


def test_budget_derivation_3g_defaults(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    # 0.5 * 3GiB / 700MiB ~= 2.19 -> floor 2
    assert bp._derive_browser_pool_size() == 2


def test_budget_derivation_capped(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(64 * 1024**3))
    # huge host -> capped at _MAX_DERIVED_POOL_SIZE (4)
    assert bp._derive_browser_pool_size() == rp._MAX_DERIVED_POOL_SIZE


def test_no_limit_falls_back_to_one(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(0))
    assert bp._derive_browser_pool_size() == 1


def test_pool_uses_derived_size(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    pool = bp.BrowserPool()
    assert pool._pool_size == 2


def test_explicit_pool_size_arg_wins(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    pool = bp.BrowserPool(pool_size=1)
    assert pool._pool_size == 1


class TestWorkerExtractorErrorDegrades:
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
        text = renderer._pdf_text_extractor(
            "http://127.0.0.1:1/nonexistent.pdf",
            UNRESTRICTED_HOST_POLICY,
        )
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
