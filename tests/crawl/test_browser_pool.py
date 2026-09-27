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
    renderer = _bare_renderer()
    context_manager = _ExplodingContextManager()
    renderer._browser = object()
    renderer._browser_cm = context_manager

    def assert_cleanly_released() -> None:
        assert renderer._browser is None
        assert renderer._browser_cm is None
        assert context_manager.exits == 1

    return renderer, context_manager, assert_cleanly_released


def _bare_renderer():
    """A real ``BrowserRenderer`` with no browser factory and no warmup.

    Every test in ``TestWorkerExtractorErrorDegrades`` drives one page-facing
    helper directly, so there is nothing to acquire and nothing to warm up. The
    renderer stays real; only the page is a double, which is what keeps these
    tests about the renderer's own error handling.
    """
    from app.crawl.strategies.browser import BrowserRenderer

    return BrowserRenderer()


def _page(**stubs):
    """A page double whose only attributes are ``stubs``.

    Each stub is the answer that method gives: a plain value is returned, an
    exception instance is raised (the error path under test), and a callable is
    called and its result returned (a page that answers differently on the
    second poll). An awaitable answer is awaited, so a stub declared ``async``
    returns the value it returns rather than the coroutine -- otherwise a
    ``if await element.is_visible():`` test would read a truthy coroutine and
    pass without ever checking the answer. A method the test did not stage is
    simply absent, so reaching for one raises ``AttributeError`` the way a page
    without it would -- rather than passing against a double that answers
    everything.
    """

    def _method(name: str, answer):
        async def _call(self, *args, **kwargs):
            if isinstance(answer, BaseException):
                raise answer
            given = answer(*args, **kwargs) if callable(answer) else answer
            if inspect.isawaitable(given):
                return await given
            return given

        _call.__name__ = name
        return _call

    return type("_StubPage", (), {name: _method(name, answer) for name, answer in stubs.items()})()


async def _challenge_wait_outcome(renderer, page):
    """Drive ``_extract_with_challenge_wait`` asking for everything.

    The render-options double is the same in every challenge-wait test: which
    page the extractor decided it was looking at is only a question if html and
    text were both requested, so the "include everything" options are staged
    once here. The renderer's own four-tuple comes back so each test asserts on
    the half it is about -- the classification on the timeout case, the escape on
    the one that gets through.
    """
    options = type("obj", (), {"include_html": True, "include_text": True})()
    return await renderer._extract_with_challenge_wait(page, options)


class TestWorkerExtractorErrorDegrades:
    """Cover remaining branches in browser.py"""

    async def test_browser_renderer_reset_browser_error(self):
        renderer, _cm, assert_released = _renderer_with_exploding_cm()
        await renderer._release_browser()
        assert_released()

    async def test_browser_renderer_close_error(self):
        renderer, _cm, assert_released = _renderer_with_exploding_cm()
        await renderer.close()
        assert_released()

    def test_browser_renderer_pdf_text_extractor_error(self):
        text = _bare_renderer()._pdf_text_extractor(
            "http://127.0.0.1:1/nonexistent.pdf",
            UNRESTRICTED_HOST_POLICY,
        )
        assert text == ""

    async def test_browser_renderer_capture_element_screenshots_error(self):
        result = await _bare_renderer()._capture_element_screenshots(_page(evaluate=[]), [])
        assert result == []

    async def test_browser_renderer_flatten_shadow_dom_error(self):
        calls = []

        def _record_then_fail(*_args, **_kwargs):
            calls.append("evaluate")
            raise Exception("flatten failed")

        await _bare_renderer()._flatten_shadow_dom(_page(evaluate=_record_then_fail))
        assert calls == ["evaluate"]

    async def test_browser_renderer_extract_with_challenge_wait_timeout(self):
        renderer = _bare_renderer()
        renderer._challenge_wait_seconds = 0.001

        # Page that always shows Cloudflare challenge
        page = _page(evaluate="Just a moment...", content="<html><body>Just a moment...</body></html>")
        _html, _text, classification, escaped = await _challenge_wait_outcome(renderer, page)
        assert classification == "bot_challenge_detected"
        assert not (escaped)

    async def test_browser_renderer_extract_with_challenge_wait_escaped(self):
        renderer = _bare_renderer()
        renderer._challenge_wait_seconds = 0.001
        polls = 0

        def _challenge_then_normal(*_args, **_kwargs):
            """One poll counter shared by evaluate and content, as a real page's is."""
            nonlocal polls
            polls += 1
            return "Just a moment..." if polls <= 3 else "Normal page content"

        def _body(*_args, **_kwargs):
            return (
                "<html><body>Just a moment...</body></html>"
                if _challenge_then_normal() == "Just a moment..."
                else "<html><body>Normal page content</body></html>"
            )

        page = _page(evaluate=_challenge_then_normal, content=_body)
        _html, _text, classification, escaped = await _challenge_wait_outcome(renderer, page)
        assert classification != "bot_challenge_detected"
        assert escaped

    async def test_browser_renderer_extract_html_error(self):
        result = await _bare_renderer()._extract_html(_page(content=Exception("content failed")))
        assert result == ""

    async def test_browser_renderer_extract_text_error(self):
        result = await _bare_renderer()._extract_text(_page(evaluate=Exception("evaluate failed")))
        assert result == ""

    async def test_browser_renderer_auto_accept_cookie_consent(self):
        # Opt-in required (M4): disabled by default, no click without consent.
        result = await _bare_renderer()._auto_accept_cookie_consent(_page(query_selector=None), False)
        assert not (result)

    async def test_browser_renderer_click_consent_button(self):
        async def _visible(*_args, **_kwargs):
            return True

        async def _click(*_args, **_kwargs):
            return None

        def _consent_query(*_args, **_kwargs):
            return _page(is_visible=_visible, click=_click)

        result = await _bare_renderer()._auto_accept_cookie_consent(
            _page(query_selector=_consent_query), True
        )
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
