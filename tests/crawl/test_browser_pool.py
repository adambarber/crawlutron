"""Browser pool: worker acquisition, timeout, sizing and global worker edges."""

import inspect

import pytest
from _policy_factories import UNRESTRICTED_HOST_POLICY

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


class _ExplodingContextManager:
    def __init__(self):
        self.exits = 0

    async def __aexit__(self, *args):
        self.exits += 1
        raise RuntimeError("teardown failed")


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


def _renderer_with_exploding_cm():
    """A renderer holding a browser whose context manager raises on exit.

    Both slots cleared and the CM exited exactly once is the contract under
    test, so the fixture asserts it and the caller only has to say *which* call
    was made.
    """
    from app.crawl.strategies.browser import BrowserRenderer

    renderer = BrowserRenderer()
    context_manager = _ExplodingContextManager()
    renderer._browser = object()
    renderer._browser_cm = context_manager

    def assert_cleanly_released() -> None:
        assert renderer._browser is None
        assert renderer._browser_cm is None
        assert context_manager.exits == 1

    return renderer, context_manager, assert_cleanly_released


class TestWorkerExtractorErrorDegrades:
    """Cover remaining branches in browser.py"""

    async def test_browser_renderer_reset_browser_error(self):
        renderer, _cm, assert_released = _renderer_with_exploding_cm()
        await renderer._reset_browser()
        assert_released()

    async def test_browser_renderer_close_error(self):
        renderer, _cm, assert_released = _renderer_with_exploding_cm()
        await renderer.close()
        assert_released()

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

        calls = []

        class MockPage:
            async def evaluate(self, _):
                calls.append("evaluate")
                raise Exception("flatten failed")

        await renderer._flatten_shadow_dom(MockPage())
        assert calls == ["evaluate"]

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


class TestCloseLeavesNoStaleRendererToAcquire:
    """close() retired the renderers but kept handing them out.

    ``close`` walked ``_renderers``, closed each and cleared the list, and left
    ``_available`` full. ``initialize`` then appended the new generation to the
    BACK of that same queue, and ``acquire`` pops the FRONT -- so the first
    ``pool_size`` acquires after a recycle returned renderers that had already
    been closed. The queue was also double the pool size, which quietly
    contradicts every ``available`` count the pool logs on timeout.
    """

    @staticmethod
    def _pool(pool_size: int = 2) -> BrowserPool:
        # A real BrowserRenderer never launches a browser at construction, so the
        # pool, its renderers and the whole queue lifecycle are exercised for
        # real; the factory is simply never called because nothing renders here.
        return BrowserPool(pool_size=pool_size, browser_factory=lambda **kwargs: None)

    async def test_acquire_after_a_recycle_returns_a_live_renderer(self):
        pool = self._pool()
        await pool.initialize()
        retired = list(pool._renderers)
        in_use = await pool.acquire()
        pool.release(in_use)

        await pool.close()
        await pool.initialize()
        live = list(pool._renderers)

        assert not set(id(r) for r in retired) & set(id(r) for r in live)
        renderer = await pool.acquire()
        try:
            assert renderer in live, "acquire returned a renderer from the closed generation"
        finally:
            pool.release(renderer)
            await pool.close()

    async def test_a_recycled_pool_holds_exactly_pool_size_renderers(self):
        pool = self._pool()
        await pool.initialize()
        await pool.close()
        await pool.initialize()
        try:
            assert pool._available.qsize() == pool._pool_size
        finally:
            await pool.close()
        assert pool._available.qsize() == 0, "close left closed renderers in the queue"

    async def test_releasing_a_renderer_after_close_does_not_resurrect_it(self):
        """The other route to the same retired renderer.

        A renderer checked out when ``close`` runs is closed by it, and the
        holder returns it afterwards. That is the one caller that can put a
        closed renderer back after the drain, so the drop belongs in ``release``.
        """
        pool = self._pool()
        await pool.initialize()
        checked_out = await pool.acquire()

        await pool.close()
        pool.release(checked_out)

        assert pool._available.qsize() == 0, "a closed renderer was returned to the queue"

        # And the live generation still accepts its own renderers.
        await pool.initialize()
        try:
            live = await pool.acquire()
            pool.release(live)
            assert pool._available.qsize() == pool._pool_size
        finally:
            await pool.close()
