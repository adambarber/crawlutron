"""The live browser: launching, recycling, tabs and driver steps.

``BrowserRenderer`` takes ``driver=``; a test passes a driver whose page steps
do nothing. The action vocabulary lives in ``app.vocabulary.browser``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from app.config.env import get_env_int
from app.transport.driver_config import (
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
from app.transport.driver_pages import (
    auto_accept_cookie_consent,
    capture_element_screenshots,
    flatten_shadow_dom,
    safe_close_page,
)
from app.vocabulary.browser import ACTIONS_REQUIRING_SELECTOR, BROWSER_ACTION_TYPES
from app.vocabulary.limits import DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS, DEFAULT_MAX_BROWSER_RENDERS

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings


logger = logging.getLogger(__name__)


async def _click_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    await page.click(str(action["selector"]))


async def _fill_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    await page.fill(str(action["selector"]), str(action.get("value", "")))


async def _hover_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    await page.hover(str(action["selector"]))


async def _scroll_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    element = await page.query_selector(str(action["selector"]))
    if element is not None:
        await element.scroll_into_view_if_needed()


async def _scroll_bottom_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")


async def _wait_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    await page.wait_for_selector(str(action["selector"]), timeout=ACTION_WAIT_MS(action))


async def _wait_seconds_verb(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    await driver._async_sleep(min(float(action.get("seconds", 1)), MAX_ACTION_WAIT_SECONDS))


async def _refuse_execute_js(driver: BrowserDriver, page: Any, action: dict[str, Any]) -> None:
    """Refuse arbitrary JS execution (RCE-class risk)."""
    return None


class _ActionStep(NamedTuple):
    handler: Callable[[BrowserDriver, Any, dict[str, Any]], Awaitable[None]]
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

    ``browser_factory`` is the Camoufox/Playwright launcher, ``max_browser_renders``
    the recycle bound that keeps a long-lived browser process from growing without
    limit, and ``cache_dir`` the allowlist a caller-supplied ``storage_state`` must
    live inside. All three default from settings, so the renderer can build one
    without restating them.
    """

    def __init__(
        self,
        *,
        browser_factory: Callable[[], Any] | None = None,
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
        # A storage_state is honoured only against an operator allowlist; the
        # platform temp dir is not one -- it is world-readable on most hosts.
        self._cache_dir = cache_dir if settings is None else settings.cache_dir

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
        self._browser_cm: Any = None
        self._browser: Any = None
        self._browser_reused = False
        self._render_count = 0

    async def open_page(self, browser_profile: dict[str, Any] | None) -> Any:
        """Return a page on a (possibly reused) browser, with profile kwargs applied.

        One call rather than acquire-then-new_page, because a caller that wants a
        page has no reason to know the browser is recycled underneath it -- and a
        test substituting this driver should not have to supply a fake browser
        whose only job is to have a ``new_page``.
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

        Public because it is a safety boundary, not plumbing: with no cache
        directory the driver accepts no storage state at all, and a caller (or a
        test) asking "is any path acceptable here?" is asking about the
        contract, not about an attribute.
        """
        return self._cache_dir

    @property
    def browser(self) -> Any:
        """The live browser, or None while the driver holds none.

        Public because releasing it is a driver contract: after ``close()`` the
        driver must be back to holding nothing, and a test asserting that should
        not have to read a private attribute to find out.
        """
        return self._browser

    def note_render(self) -> None:
        """Count one render attempt, successful or not, against the recycle bound."""
        self._render_count += 1

    async def close(self) -> None:
        """Drop the cached browser. Best-effort, and safe to call more than once."""
        await self._release_browser()

    def create_camoufox_browser(self, **kwargs: Any):
        """Factory for AsyncCamoufox browser."""
        return create_camoufox_browser(**kwargs)

    def headless_enabled(self) -> bool:
        return headless_enabled()

    def validate_storage_state_path(self, storage_state: str) -> str | None:
        """A storage_state path under the cache dir, or None."""
        return validate_storage_state_path(storage_state, self._cache_dir)

    def browser_kwargs(self, browser_profile: dict[str, Any] | None) -> dict[str, Any]:
        """Launch-only kwargs for the Camoufox/Playwright browser factory."""
        return browser_kwargs(browser_profile, self._cache_dir)

    def page_kwargs(self, browser_profile: dict[str, Any] | None) -> dict[str, Any]:
        """Page/context kwargs, applied at ``browser.new_page`` time."""
        return page_kwargs(browser_profile)

    async def _acquire_browser(self, browser_profile: dict[str, Any] | None) -> Any:
        """Return a reused browser instance, launching one on first use.

        Recycles the browser once it has served ``_max_browser_renders`` to
        bound memory growth of a long-lived Camoufox process.
        """
        if (
            self._browser is None
            or getattr(self._browser, "is_closed", False)
            or self._render_count >= self._max_browser_renders
        ):
            if self._browser is not None:
                await self._release_browser()
            launch_kwargs = self.browser_kwargs(browser_profile)
            cm = self._browser_factory(**launch_kwargs)
            self._browser = await cm.__aenter__()
            self._browser_cm = cm
            self._browser_reused = False
            self._render_count = 0
        else:
            self._browser_reused = True
        return self._browser

    async def _release_browser(self) -> None:
        """Take the cached context manager and tear it down, best-effort.

        The single owner of "forget the browser": both the per-attempt reset
        and the service-shutdown close have to drop the same three fields, and
        when they were spelled separately one of them grew a stale
        ``_browser_reused`` that the other did not clear. Clearing the whole
        set here means a field added later is cleared in both places or in
        neither, which is the only way that stays true.
        """
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        self._browser_reused = False
        if cm is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                await cm.__aexit__(None, None, None)

    async def wait_for_ready_state(self, page: Any) -> None:
        for _ in range(10):
            state = await page.evaluate("() => document.readyState")
            if state in {"interactive", "complete"}:
                return
            await self._async_sleep(0.1)

    async def flatten_shadow_dom(self, page: Any) -> None:
        await flatten_shadow_dom(page)

    async def auto_accept_cookie_consent(self, page: Any, enabled: bool) -> bool:
        """Auto-click cookie consent buttons when the caller opts in."""
        return await auto_accept_cookie_consent(page, enabled, self._sleep)

    async def run_action_chain(
        self,
        page: Any,
        actions: list[dict[str, Any]],
        *,
        url: str = "",
        budget_seconds: float | None = None,
    ) -> None:
        """Run policy-specified actions under ONE deadline, then move on.

        The chain had no deadline at all -- only the navigation above it was
        bounded -- so N ``wait`` actions held the page semaphore (one slot of the
        pool that exists because concurrent tabs crash the shared process) for
        N x ``MAX_ACTION_WAIT_SECONDS``, and the exhaustion that followed said
        nothing about the chain still waiting. The budget is the per-URL number
        the navigation already got, so the render's worst case stops growing
        with the chain length.

        ``budget_seconds=None`` is the unbounded form, for driving the chain
        directly; production must pass the render budget, which
        ``test_the_action_chain_is_always_invoked_with_a_budget`` enforces
        tree-wide, since an optional deadline is only as good as that gate.
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

    async def _perform_action_chain(self, page: Any, actions: list[dict[str, Any]], *, url: str) -> None:
        """Execute a sequence of browser actions before extraction.

        Supported action types: ``click``, ``fill``, ``hover``, ``scroll``
        (into an element), ``scroll_bottom``, ``wait`` (for a selector), and
        ``wait_seconds``.

        Arbitrary JavaScript (``execute_js``) is intentionally NOT supported:
        running caller-supplied scripts in the browser context is an RCE-class
        risk (cookie/localStorage theft, requests to internal services with no
        SSRF guard).

        Actions are best-effort -- one bad selector must not cost the page its
        extraction -- but not silent, since a ``wait`` for an element a SPA
        never grows is an invisible extraction-quality loss.
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

    async def _dispatch_action(self, page: Any, action: dict[str, Any], action_type: str) -> None:
        """Perform ONE recognised action via :data:`_ACTION_STEPS`.

        ``execute_js`` is recognised and refused here as well as at the model,
        because this is the chokepoint a policy arriving by any other route
        still cannot run script through.
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

    async def capture_element_screenshots(self, page: Any, selectors: list[str]) -> list[dict[str, Any]]:
        """Capture targeted element screenshots as base64."""
        return await capture_element_screenshots(page, selectors)

    async def safe_close_page(self, page: Any) -> None:
        """Best-effort page teardown."""
        await safe_close_page(page)

    async def lazy_load_scroll(self, page: Any, browser_profile: dict[str, Any] | None = None) -> None:
        """Scroll to the bottom repeatedly to trigger lazy-loaded content."""
        steps, delay = self.scroll_config_for(browser_profile)
        for _ in range(steps):
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:  # scrolling is best-effort
                return
            await self._async_sleep(delay)

    def harvest_timeout_for(self, browser_profile: dict[str, Any] | None) -> float:
        """Cookie-harvest wait bound: policy profile beats settings."""
        return harvest_timeout_for(browser_profile, self._harvest_timeout_seconds)

    def scroll_config_for(self, browser_profile: dict[str, Any] | None) -> tuple[int, float]:
        """Lazy-scroll steps/delay: policy profile beats settings."""
        return scroll_config_for(browser_profile, self._lazy_scroll_steps, self._lazy_scroll_delay_seconds)

    async def _async_sleep(self, seconds: float) -> None:
        """Wait between page steps, off the event loop. See :func:`async_sleep`."""
        await async_sleep(self._sleep, seconds)
