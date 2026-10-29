"""The live browser: launching, recycling, tabs and driver steps.

``BrowserRenderer`` takes ``driver=`` since a test passes a driver whose page
steps do nothing; the action vocabulary lives in ``crawlutron.vocabulary.browser``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast

from crawlutron.config.env import get_env_int
from crawlutron.models.browser_profile_record import BrowserProfileRecord
from crawlutron.models.screenshot_diagnostics import ScreenshotDiagnostics
from crawlutron.transport.driver_config import (
    ACTION_WAIT_MS,
    LAZY_SCROLL_DELAY_SECONDS,
    LAZY_SCROLL_STEPS,
    MAX_ACTION_WAIT_SECONDS,
    async_sleep,
    browser_kwargs,
    create_camoufox_browser,
    harvest_timeout_for,
    headless_enabled,
    page_kwargs,
    scroll_config_for,
    validate_storage_state_path,
)
from crawlutron.transport.driver_pages import (
    auto_accept_cookie_consent,
    capture_element_screenshots,
    flatten_shadow_dom,
    safe_close_page,
)
from crawlutron.transport.render_page import RenderPage
from crawlutron.vocabulary.browser import ACTIONS_REQUIRING_SELECTOR, BROWSER_ACTION_TYPES, BrowserAction
from crawlutron.vocabulary.limits import DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS, DEFAULT_MAX_BROWSER_RENDERS

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings


logger = logging.getLogger(__name__)


class BrowserHandle(Protocol):
    """What the driver does with a launched browser: open pages. Closure is
    probed duck-typed (:func:`_browser_closed`), because the real Playwright
    ``Browser`` publishes no ``is_closed`` and a double may."""

    async def new_page(self, **kwargs: object) -> RenderPage: ...


class BrowserContextManager(Protocol):
    """The ``async with`` wrapper a browser factory hands back.

    AsyncCamoufox yields ``Browser | BrowserContext``; the driver only calls
    ``new_page`` on it, so the handle is the protocol member, not the class.
    """

    async def __aenter__(self) -> object: ...

    async def __aexit__(self, *exc: object) -> object: ...


async def _click_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    await page.click(str(action.get("selector", "")))


async def _fill_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    await page.fill(str(action.get("selector", "")), str(action.get("value", "")))


async def _hover_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    await page.hover(str(action.get("selector", "")))


async def _scroll_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    element = await page.query_selector(str(action.get("selector", "")))
    if element is not None:
        await element.scroll_into_view_if_needed()


async def _scroll_bottom_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")


async def _wait_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    await page.wait_for_selector(str(action.get("selector", "")), timeout=ACTION_WAIT_MS(action))


async def _wait_seconds_verb(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    await driver._async_sleep(min(float(action.get("seconds", 1)), MAX_ACTION_WAIT_SECONDS))


async def _refuse_execute_js(driver: BrowserDriver, page: RenderPage, action: BrowserAction) -> None:
    """Refuse arbitrary JS execution (RCE-class risk)."""
    return None


class _ActionStep(NamedTuple):
    handler: Callable[[BrowserDriver, RenderPage, BrowserAction], Awaitable[None]]
    needs_selector: bool


#: The bounded vocabulary the chain executes. ``needs_selector`` derives from
#: the shared vocabulary so the table and the model refusal cannot drift.
ACTION_STEPS: dict[str, _ActionStep] = {
    name: _ActionStep(handler, name in ACTIONS_REQUIRING_SELECTOR)
    for name, handler in (
        ("click", _click_verb),
        ("fill", _fill_verb),
        ("hover", _hover_verb),
        ("scroll", _scroll_verb),
        ("scroll_bottom", _scroll_bottom_verb),
        ("wait", _wait_verb),
        ("wait_seconds", _wait_seconds_verb),
        ("execute_js", _refuse_execute_js),
    )
}


class BrowserDriver:
    """Owns the browser and does page work; the renderer decides what to ask for.
    ``browser_factory`` (launcher), ``max_browser_renders`` (recycle bound) and
    ``cache_dir`` (storage_state allowlist) default from settings, so the renderer builds one freely.
    """

    def __init__(
        self,
        *,
        browser_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_browser_renders: int | None = None,
        cache_dir: str | Path | None = None,
        lazy_scroll_steps: int = LAZY_SCROLL_STEPS,
        lazy_scroll_delay_seconds: float = LAZY_SCROLL_DELAY_SECONDS,
        harvest_timeout_seconds: float = DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS,
        settings: CrawlerSettings | None = None,
    ) -> None:
        self._browser_factory = browser_factory or self.create_camoufox_browser
        self._sleep = sleep
        self._max_browser_renders = (
            max_browser_renders
            if max_browser_renders is not None
            else (
                settings.max_browser_renders
                if settings is not None
                else get_env_int("CRAWLUTRON_MAX_BROWSER_RENDERS", DEFAULT_MAX_BROWSER_RENDERS, lo=1)
            )
        )
        # An explicit ``cache_dir=`` beats the settings default, so passing it
        # alongside ``settings`` honors the caller's path instead.
        self._cache_dir = (
            cache_dir if cache_dir is not None else (settings.cache_dir if settings is not None else None)
        )

        self._lazy_scroll_steps = (
            lazy_scroll_steps if settings is None else int(settings.browser_lazy_scroll_steps)
        )
        self._lazy_scroll_delay_seconds = (
            lazy_scroll_delay_seconds
            if settings is None
            else float(settings.browser_lazy_scroll_delay_seconds)
        )
        self._harvest_timeout_seconds = (
            harvest_timeout_seconds
            if settings is None
            else float(settings.browser_cookie_harvest_timeout_seconds)
        )
        self._browser_cm: BrowserContextManager | None = None
        self._browser: BrowserHandle | None = None
        self._browser_reused = False
        self._render_count = 0

    async def open_page(self, browser_profile: BrowserProfileRecord | None) -> RenderPage:
        """Return a page on a (possibly reused) browser, with profile kwargs applied.
        One call rather than acquire-then-new_page, so a test substituting this
        driver does not have to supply a fake browser whose only job is ``new_page``.
        """
        browser = await self._acquire_browser(browser_profile)
        return await browser.new_page(**self.page_kwargs(browser_profile))

    @property
    def reused(self) -> bool:
        """Whether the current page was opened on a browser already serving earlier renders."""
        return self._browser_reused

    @property
    def cache_dir(self) -> str | Path | None:
        """Where storage-state files are allowed to live, or None when unset.
        Public because it is a safety boundary: with no cache dir the driver accepts
        no storage state at all, so "is any path acceptable?" is a contract question.
        """
        return self._cache_dir

    @property
    def browser(self) -> BrowserHandle | None:
        """The live browser, or None while the driver holds none.
        Public because releasing it is a driver contract: after ``close()`` the driver
        must hold nothing, and a test asserting that reads a public property.
        """
        return self._browser

    def note_render(self) -> None:
        """Count one render attempt, successful or not, against the recycle bound."""
        self._render_count += 1

    async def close(self) -> None:
        """Drop the cached browser. Best-effort, and safe to call more than once."""
        await self._release_browser()

    def create_camoufox_browser(self, **kwargs: object):
        """Factory for AsyncCamoufox browser."""
        return create_camoufox_browser(**kwargs)

    def headless_enabled(self) -> bool:
        return headless_enabled()

    def validate_storage_state_path(self, storage_state: str) -> str | None:
        """A storage_state path under the cache dir, or None."""
        return validate_storage_state_path(storage_state, self._cache_dir)

    def browser_kwargs(self, browser_profile: BrowserProfileRecord | None) -> dict[str, object]:
        """Launch-only kwargs for the Camoufox/Playwright browser factory."""
        return browser_kwargs(browser_profile, self._cache_dir)

    def page_kwargs(self, browser_profile: BrowserProfileRecord | None) -> dict[str, object]:
        """Page/context kwargs, applied at ``browser.new_page`` time."""
        return page_kwargs(browser_profile)

    async def _acquire_browser(self, browser_profile: BrowserProfileRecord | None) -> BrowserHandle:
        """Return a reused browser instance, launching one on first use.

        Recycles the browser once it has served ``_max_browser_renders`` to
        bound memory growth of a long-lived Camoufox process.
        """
        if (
            self._browser is None
            or _browser_closed(self._browser)
            or self._render_count >= self._max_browser_renders
        ):
            if self._browser is not None:
                await self._release_browser()
            launch_kwargs = self.browser_kwargs(browser_profile)
            cm = self._browser_factory(**launch_kwargs)
            # The yielded handle is object-typed in the wild (Browser|BrowserContext);
            # the driver only calls new_page on it, so cast to the handle here.
            browser = cast(BrowserHandle, await cm.__aenter__())
            self._browser = browser
            self._browser_cm = cm
            self._browser_reused = False
            self._render_count = 0
        else:
            self._browser_reused = True
        browser = self._browser
        if browser is None:  # unreachable: both arms above assign it
            raise AssertionError("_acquire_browser left no browser")
        return browser

    async def _release_browser(self) -> None:
        """Take the cached context manager and tear it down, best-effort.
        The single owner of "forget the browser": both reset and close paths must
        clear the same fields, and clearing them once here keeps that true.
        """
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        self._browser_reused = False
        if cm is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                await cm.__aexit__(None, None, None)

    async def wait_for_ready_state(self, page: RenderPage) -> None:
        for _ in range(10):
            state = await page.evaluate("() => document.readyState")
            if state in {"interactive", "complete"}:
                return
            await self._async_sleep(0.1)

    async def flatten_shadow_dom(self, page: RenderPage) -> None:
        await flatten_shadow_dom(page)

    async def auto_accept_cookie_consent(self, page: RenderPage, enabled: bool) -> bool:
        """Auto-click cookie consent buttons when the caller opts in."""
        return await auto_accept_cookie_consent(page, enabled, self._sleep)

    async def run_action_chain(
        self,
        page: RenderPage,
        actions: list[BrowserAction],
        *,
        url: str = "",
        budget_seconds: float | None = None,
    ) -> None:
        """Run policy-specified actions under ONE deadline, then move on.
        A chain without one let N waits hold a pooled page for N x MAX_ACTION_WAIT_SECONDS,
        so the render budget bounds it (None is the unbounded test form).
        """
        if not actions:
            return
        if budget_seconds is None:
            await self._perform_action_chain(page, actions, url=url)
            return
        try:
            await asyncio.wait_for(self._perform_action_chain(page, actions, url=url), timeout=budget_seconds)
        except TimeoutError:
            logger.warning(
                "browser_action_chain_budget_exhausted",
                extra={
                    "url": url,
                    "budget_seconds": budget_seconds,
                    "actions": len(actions),
                },
            )

    async def _perform_action_chain(
        self, page: RenderPage, actions: list[BrowserAction], *, url: str
    ) -> None:
        """Execute a sequence of browser actions before extraction: click, fill, hover,
        scroll, scroll_bottom, wait, wait_seconds. ``execute_js`` is refused (RCE-class
        risk); actions are best-effort but not silent -- a wait that never resolves is a real quality loss.
        """
        for action in actions:
            if not isinstance(action, dict):
                logger.warning("browser_action_skipped", extra={"reason": "action_not_an_object"})
                continue
            action_type = str(action.get("type", "")).lower()
            if action_type not in BROWSER_ACTION_TYPES:
                # The model boundary refuses an unknown type, so reaching this is a
                # bypass; log it -- a silently dropped action is a no-op page load.
                logger.warning(
                    "browser_action_skipped",
                    extra={"reason": "unknown_action_type", "action_type": action_type},
                )
                continue
            try:
                await self._dispatch_action(page, action, action_type)
            except Exception as error:  # action chains are best-effort, not silent
                logger.warning(
                    "browser_action_failed",
                    extra={
                        "url": url,
                        "action": action_type,
                        "selector": action.get("selector"),
                        "wait_ms": action.get("timeout"),
                        "error": str(error),
                    },
                )

    async def _dispatch_action(self, page: RenderPage, action: BrowserAction, action_type: str) -> None:
        """Perform ONE recognised action via :data:`_ACTION_STEPS`.
        ``execute_js`` is recognised and refused here as at the model, because this
        is the chokepoint a policy arriving by any route cannot run script through.
        """
        step = ACTION_STEPS.get(action_type)
        if step is None:
            return
        if step.needs_selector and not action.get("selector"):
            logger.warning(
                "browser_action_skipped", extra={"reason": "missing_selector", "action": action_type}
            )
            return
        await step.handler(self, page, action)

    async def capture_element_screenshots(
        self, page: RenderPage, selectors: list[str]
    ) -> list[ScreenshotDiagnostics]:
        """Capture targeted element screenshots as base64."""
        return await capture_element_screenshots(page, selectors)

    async def safe_close_page(self, page: RenderPage) -> None:
        """Best-effort page teardown."""
        await safe_close_page(page)

    async def lazy_load_scroll(
        self, page: RenderPage, browser_profile: BrowserProfileRecord | None = None
    ) -> None:
        """Scroll to the bottom repeatedly to trigger lazy-loaded content.

        The scroll evaluate returns the page height; two stable polls stop the
        pass early so a static page pays no fixed-delay tax."""
        steps, delay = self.scroll_config_for(browser_profile)
        previous_height: float | None = None
        stable_polls = 0
        for _ in range(steps):
            try:
                height = await page.evaluate(
                    "() => { const h = document.body.scrollHeight; window.scrollTo(0, h); return h; }"
                )
            except Exception:  # scrolling is best-effort
                return
            if isinstance(height, (int, float)):
                stable_polls = stable_polls + 1 if height == previous_height else 0
                previous_height = height
                if stable_polls >= 2:
                    return
            await self._async_sleep(delay)

    def harvest_timeout_for(self, browser_profile: BrowserProfileRecord | None) -> float:
        """Cookie-harvest wait bound: policy profile beats settings."""
        return harvest_timeout_for(browser_profile, self._harvest_timeout_seconds)

    def scroll_config_for(self, browser_profile: BrowserProfileRecord | None) -> tuple[int, float]:
        """Lazy-scroll steps/delay: policy profile beats settings."""
        return scroll_config_for(browser_profile, self._lazy_scroll_steps, self._lazy_scroll_delay_seconds)

    async def _async_sleep(self, seconds: float) -> None:
        """Wait between page steps, off the event loop. See :func:`async_sleep`."""
        await async_sleep(self._sleep, seconds)


def _browser_closed(browser: object) -> bool:
    """Whether ``browser`` is closed, calling an ``is_closed`` method (Playwright)
    or reading a bool attribute/property, and defaulting to open when it publishes none.
    """
    probe = getattr(browser, "is_closed", False)
    if callable(probe):
        return bool(probe())
    return bool(probe)
