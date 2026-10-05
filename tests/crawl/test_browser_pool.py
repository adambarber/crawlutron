"""Browser pool: worker acquisition, timeout, sizing and global worker edges."""

import inspect

import pytest

import app.crawl.strategies.browser_pool as bp
from app import resource_pressure as rp
from app.crawl.strategies.browser_driver import BrowserDriver
from app.crawl.strategies.browser_pool import (
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS,
    BrowserPool,
    BrowserPoolExhaustedError,
    close_browser_pool,
    get_browser_pool,
    set_browser_pool,
)
from tests._browser_doubles import FakeBrowserPool
from tests._policy_factories import canonical


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


class _ExplodingContextManager:
    def __init__(self):
        self.exits = 0
        self.enters = 0

    async def __aenter__(self):
        self.enters += 1
        return _BrowserDouble()

    async def __aexit__(self, *args):
        self.exits += 1
        raise RuntimeError("teardown failed")


class _BrowserDouble:
    """The minimum a driver needs from a browser: one page, per open."""

    def __init__(self):
        self.is_closed = False

    async def new_page(self, **_kwargs):
        return _page()


def _clear():
    set_browser_pool(None)


def test_get_browser_pool_returns_none_before_initialize():
    # Contract: calling getters before startup must NOT silently construct a
    # BrowserPool with settings-unaware default size. It must be None.
    _clear()
    assert get_browser_pool() is None
    _clear()


def test_get_browser_pool_returns_set_pool():
    stub = FakeBrowserPool()
    try:
        set_browser_pool(stub)
        assert get_browser_pool() is stub
    finally:
        _clear()


@pytest.mark.asyncio
async def test_close_browser_pool_nulls_global():
    stub = FakeBrowserPool()
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
    monkeypatch.delenv("CRAWLUTRON_BROWSER_POOL_SIZE", raising=False)
    yield


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "3")
    assert bp._derive_browser_pool_size() == 3


def test_explicit_override_rejects_garbage(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "not-a-number")
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    # garbled override -> falls through to budget derivation (3GiB -> 2)
    assert bp._derive_browser_pool_size() == 2


@pytest.mark.parametrize("raw", ["0", "-5", "  "])
def test_nonpositive_or_blank_override_falls_through(monkeypatch, raw):
    # An override that cannot mean a pool size (zero, negative, blank) is
    # garbage, not a clamp-to-1 request: derivation owns the sizing.
    monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", raw)
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


def _driver_with_exploding_cm(*, max_browser_renders: int = 1):
    """A driver holding a browser whose context manager raises on exit.

    A released browser (no cached one left) and exactly one exit of the context
    manager is the contract under test, so the fixture asserts both and the
    caller only has to say *which* call was made.
    """
    context_manager = _ExplodingContextManager()
    driver = BrowserDriver(
        browser_factory=lambda **_kwargs: context_manager,
        max_browser_renders=max_browser_renders,
    )

    def assert_cleanly_released() -> None:
        assert driver.browser is None
        assert context_manager.exits == 1

    return driver, context_manager, assert_cleanly_released


def _bare_renderer(**overrides):
    """A real ``BrowserRenderer`` with no browser factory and no warmup.

    Only the renderer-owned collaborators are exercised through this; page-facing
    steps go to :func:`_bare_driver`, which owns them. Both stay real -- only the
    page is a double, which is what keeps these tests about the strategy's own
    error handling rather than a stub's.
    """
    from app.crawl.strategies.browser import BrowserRenderer

    return BrowserRenderer(**overrides)


def _bare_driver(**overrides):
    """The driver's counterpart to :func:`_bare_renderer`, for the page-facing steps."""
    return BrowserDriver(**overrides)


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
    return await renderer.extract_with_challenge_wait(page, options)


class TestWorkerExtractorErrorDegrades:
    """The error branches of the browser strategy, across renderer and driver.

    Split by owner rather than by file: the split moved half of these onto the
    driver, and naming the class after the module it used to live in is how the
    retarget left five driver tests wearing a renderer's name.
    """

    async def test_recycle_releases_the_browser_it_replaces(self):
        """The per-render recycle path must drop the browser it is replacing.

        A separate entry point from shutdown: the driver releases the old
        context manager on the way to launching a replacement, so a raise there
        has to be swallowed the same way or every recycle after the first
        browser breaks the render.
        """
        driver, cm, _ = _driver_with_exploding_cm(max_browser_renders=1)
        await driver.open_page({})
        driver.note_render()
        await driver.open_page({})
        assert cm.exits == 1, "the recycle path did not release the browser it replaced"

    async def test_close_releases_browser_when_teardown_raises(self):
        driver, _cm, assert_released = _driver_with_exploding_cm()
        await driver.open_page({})
        await driver.close()
        assert_released()

    def test_browser_renderer_pdf_text_extractor_error(self):
        text = _bare_renderer().pdf_text_extractor(canonical("http://127.0.0.1:1/nonexistent.pdf"))
        assert text == ""

    async def test_driver_capture_element_screenshots_error(self):
        calls = []

        def _record_then_fail(*_args, **_kwargs):
            calls.append("query_selector")
            raise Exception("capture failed")

        result = await _bare_driver().capture_element_screenshots(
            _page(query_selector=_record_then_fail), ["h1"]
        )
        assert result == [], "a best-effort per-element capture must swallow the failure"
        # The empty result is true of any implementation unless the loop is
        # known to have run. Passing an empty selector list made this test
        # assert nothing at all, and it had been green for exactly that reason.
        assert calls == ["query_selector"], "the selector was never attempted"

    async def test_driver_flatten_shadow_dom_error(self):
        calls = []

        def _record_then_fail(*_args, **_kwargs):
            calls.append("evaluate")
            raise Exception("flatten failed")

        await _bare_driver().flatten_shadow_dom(_page(evaluate=_record_then_fail))
        assert calls == ["evaluate"]

    async def test_browser_renderer_extract_with_challenge_wait_timeout(self):
        renderer = _bare_renderer(challenge_wait_seconds=0.001)

        # Page that always shows Cloudflare challenge
        page = _page(evaluate="Just a moment...", content="<html><body>Just a moment...</body></html>")
        _html, _text, classification, escaped = await _challenge_wait_outcome(renderer, page)
        assert classification == "bot_challenge_detected"
        assert not (escaped)

    async def test_browser_renderer_extract_with_challenge_wait_escaped(self):
        renderer = _bare_renderer(challenge_wait_seconds=0.001)
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
        result = await _bare_renderer().extract_html(_page(content=Exception("content failed")))
        assert result == ""

    async def test_browser_renderer_extract_text_error(self):
        result = await _bare_renderer().extract_text(_page(evaluate=Exception("evaluate failed")))
        assert result == ""

    async def test_driver_auto_accept_cookie_consent(self):
        # Opt-in required: disabled by default, no click without consent.
        result = await _bare_driver().auto_accept_cookie_consent(_page(query_selector=None), False)
        assert not (result)

    async def test_driver_click_consent_button(self):
        async def _visible(*_args, **_kwargs):
            return True

        async def _click(*_args, **_kwargs):
            return None

        def _consent_query(*_args, **_kwargs):
            return _page(is_visible=_visible, click=_click)

        result = await _bare_driver().auto_accept_cookie_consent(_page(query_selector=_consent_query), True)
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
        # real; nothing renders here, so no browser factory is ever called and
        # none needs supplying. (The renderer no longer accepts one anyway --
        # controlling the browser means building a driver and passing it.)
        return BrowserPool(pool_size=pool_size)

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
