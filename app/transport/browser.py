"""Browser rendering: a headless browser for JavaScript-heavy pages."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import TYPE_CHECKING, Any, Literal

from app.config.env import get_env_float, get_env_int
from app.config.settings import CrawlerSettings
from app.extraction.classify import classify_page
from app.models.crawl_result import CrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions
from app.models.taxonomy import Classification, CrawlError
from app.orchestration.classification import classify_error_code
from app.orchestration.render_result import (
    terminal_attempt,
    terminal_crawl_result,
)
from app.transport.browser_cookies import (
    _BROWSER_COOKIES_BY_DOMAIN,
    _BROWSER_COOKIES_MAX,
    _harvest_session_cookies,
    _store_browser_cookies,
    get_browser_cookies_for,
    has_process_state,
    reset_process_state,
)
from app.transport.browser_driver import BrowserDriver, async_sleep
from app.transport.browser_targets import (
    make_response_handler,
    make_ssrf_route_handler,
    make_ssrf_websocket_handler,
    resolve_browser_target,
)

__all__ = [
    "BrowserRenderer",
    "_BROWSER_COOKIES_BY_DOMAIN",
    "_BROWSER_COOKIES_MAX",
    "_store_browser_cookies",
    "get_browser_cookies_for",
    "has_process_state",
    "make_response_handler",
    "make_ssrf_route_handler",
    "make_ssrf_websocket_handler",
    "reset_process_state",
    "resolve_browser_target",
]
from app.transport.document import _default_document_text_extractor
from app.transport.pinned_transport import build_pinned_request, build_pinned_request_async
from app.vocabulary.limits import DEFAULT_CHALLENGE_WAIT_SECONDS, DEFAULT_MAX_CONCURRENT_PAGES
from app.vocabulary.taxonomy import ResultStatus, status_for_classification

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


class BrowserRenderer:
    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        challenge_wait_seconds: float | None = None,
        pdf_text_extractor: Callable[[NormalizedUrl], str] | None = None,
        extract: Callable[
            [Any, RenderOptions], Awaitable[tuple[str | None, str | None, Classification, bool]]
        ]
        | None = None,
        looks_like_pdf: Callable[[str], bool] | None = None,
        warmup_urls: list[str] | None = None,
        settings: CrawlerSettings | None = None,
        driver: BrowserDriver | None = None,
        pin: Callable[..., Awaitable[tuple[str, dict[str, str]]]] | None = None,
        pin_sync: Callable[..., tuple[str, dict[str, str]]] | None = None,
        status_for: Callable[[str], ResultStatus] | None = None,
    ) -> None:
        self._sleep = sleep
        # Policy limit, then the settings field, then env, then the canonical
        # default: the number itself lives only in app.vocabulary.limits.
        if challenge_wait_seconds is None:
            if settings is not None:
                self._challenge_wait_seconds = float(settings.challenge_wait_seconds)
            else:
                self._challenge_wait_seconds = get_env_float(
                    "CRAWLUTRON_CHALLENGE_WAIT_SECONDS", DEFAULT_CHALLENGE_WAIT_SECONDS, lo=0.0, hi=300.0
                )
        else:
            self._challenge_wait_seconds = challenge_wait_seconds
        self._pdf_text_extractor = pdf_text_extractor or _default_document_text_extractor
        # The two renderer-owned predicates a test steers (page yield, PDF);
        # overridable collaborators, so a test substitutes behaviour, not internals.
        self._extract = extract or self.extract_with_challenge_wait
        self._looks_like = looks_like_pdf or self._looks_like_pdf
        self._warmup_urls = warmup_urls if warmup_urls is not None else self._warmup_urls_from_env()
        # The two SSRF pin rules and the failure-path classifier: all three decide
        # something the render cannot recover from, so tests steer them by argument.
        self._pin = pin or build_pinned_request_async
        self._pin_sync = pin_sync or build_pinned_request
        self._status_for = status_for or status_for_classification
        # The browser and every page step live in the driver (recycle bound, scroll/
        # harvest knobs, storage_state allowlist); a test substitutes the driver.
        self._driver = driver if driver is not None else BrowserDriver(sleep=sleep, settings=settings)
        # Cap concurrent page renders: N simultaneous requests each open a tab,
        # which otherwise crashes/OOMs the browser under burst load.
        _concurrent_pages = (
            settings.max_concurrent_pages
            if settings is not None
            else get_env_int("CRAWLUTRON_MAX_CONCURRENT_PAGES", DEFAULT_MAX_CONCURRENT_PAGES, lo=1)
        )
        self._page_semaphore = asyncio.Semaphore(max(1, _concurrent_pages))

    async def _async_sleep(self, seconds: float) -> None:
        """Non-blocking sleep for use inside coroutines.

        The default ``time.sleep`` clock (injectable for tests) would block the
        whole event loop; run it in a thread so one render can never freeze the
        service.
        """
        await async_sleep(self._sleep, seconds)

    @property
    def pdf_text_extractor(self) -> Callable[[NormalizedUrl], str]:
        """The collaborator that turns a PDF URL into text.

        Public because it is an injection point: a caller that supplied one, or a
        test asserting the supplied/default behaviour, should not have to read a
        private attribute to say which extractor is in play.
        """
        return self._pdf_text_extractor

    @property
    def driver(self) -> BrowserDriver:
        """The driver this renderer drives the page with.

        Public because the driver is a collaborator rather than an
        implementation detail: it owns the browser lifecycle and the per-page
        steps, and a test asserting on either needs to name it rather than reach
        past the renderer into a private attribute.
        """
        return self._driver

    @property
    def warmup_urls(self) -> list[str]:
        """Resolved warmup URLs for this renderer's browser profile.

        The headed-warmed strategy profile is the one carrying warmup URLs (see
        ``app.vocabulary.strategies.BROWSER_STRATEGIES``); exposing the resolved list lets
        the browser pool label its renders with the real browser mode instead
        of inventing a strategy name on a failure path.
        """
        return self._warmup_urls

    async def render(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        started = time.monotonic()
        attempts = 0
        last_error = None
        max_attempts = max(1, options.max_retries + 1)

        while attempts < max_attempts:
            attempts += 1
            try:
                result = await self._render_with_browser(url, options, policy=policy)
                return result
            except Exception as error:
                classification: Literal["navigation_timeout", "unknown_error"] = (
                    "navigation_timeout" if isinstance(error, TimeoutError) else "unknown_error"
                )
                last_error = classify_error_code(error)
                await self._driver.close()
                if attempts >= max_attempts:
                    return self._failure_result(
                        url, "retry_exhausted", classification, attempts, started, last_error
                    )
                await self._async_sleep(min(2 ** (attempts - 1), 5))

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    async def close(self) -> None:
        """Release the reused browser instance at service shutdown."""
        await self._driver.close()

    async def _render_with_browser(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        # Cap concurrent page renders: each render opens a browser tab, and an
        # unbounded burst of tabs crashes/OOMs the shared Camoufox process.
        async with self._page_semaphore:
            return await self._render_with_page_unlocked(url, options, policy=policy)

    async def _navigate_and_settle(self, page: Any, pinned_url: str) -> None:
        """Goto the pinned URL and wait for a settled ready state."""
        await page.goto(pinned_url, wait_until="domcontentloaded")
        await self._driver.wait_for_ready_state(page)

    async def _render_with_page_unlocked(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        # SSRF: validate + IP-pin the target (DNS-rebinding safe, Host preserved).
        # Raises ValueError if the target is unsafe.
        pinned_url, goto_headers = await resolve_browser_target(url, pin=self._pin)

        started = time.monotonic()

        page = await self._driver.open_page(options.browser_profile)
        # Abort any request (navigation, redirect, or subresource) that
        # resolves to an SSRF-unsafe host before the browser connects.
        await page.route(
            "**/*",
            make_ssrf_route_handler(
                (options.browser_profile or {}).get("resource_mode"),
                include_screenshot=bool((options.browser_profile or {}).get("include_screenshot")),
                policy=policy,
                pin=self._pin_sync,
            ),
        )
        ws_guard = make_ssrf_websocket_handler(policy=policy)
        ws_router = getattr(page, "route_websocket", None)
        if ws_router is None:
            logger.warning("websocket_interception_unsupported")
        else:
            # Route_websocket is sync in Playwright (unlike page.route); accept
            # both sync and async implementations for test doubles.
            outcome = ws_router("**/*", ws_guard)
            if asyncio.iscoroutine(outcome):
                await outcome
        try:
            # Katana-style XHR/fetch interception.
            intercepted_xhr: list[dict[str, Any]] = []
            page.on("response", make_response_handler(intercepted_xhr))

            # Warmup URLs (no Host header needed). The driver's goto takes
            # text -- .raw projects at that external-API edge.
            for warmup_url in self._warmup_urls:
                normalized_warmup = NormalizedUrl(warmup_url, policy=policy)
                await page.goto(normalized_warmup.raw, wait_until="domcontentloaded")
                await self._driver.wait_for_ready_state(page)

            # Navigate with the Host override via set_extra_http_headers (goto takes no
            # headers); wait_for honors the timeout, floored at 1s, mapping to navigation_timeout.
            navigation_timeout = max(1.0, float(options.timeout_seconds))
            if goto_headers:
                await page.set_extra_http_headers(goto_headers)
            await asyncio.wait_for(
                self._navigate_and_settle(page, pinned_url),
                timeout=navigation_timeout,
            )
            if goto_headers:
                # Clear the page-scoped Host override: leaving it would spoof Host
                # on every later request from this page (XHR, later navigations).
                await page.set_extra_http_headers({})

            # Browser action chains: run policy-specified interactions before extraction.
            actions = (options.browser_profile or {}).get("actions") or []
            if actions:
                await self._driver.run_action_chain(
                    page, actions, url=url.raw, budget_seconds=navigation_timeout
                )

            # Lazy-load scroll: trigger deferred/infinite-scroll content.
            await self._driver.lazy_load_scroll(page, options.browser_profile)

            # Shadow-DOM flattening: inline open shadow roots so their links
            # and visible text are reachable by the standard HTML/text extractors.
            await self._driver.flatten_shadow_dom(page)

            # Auto-accept cookie consent (opt-in only) before extraction
            await self._driver.auto_accept_cookie_consent(
                page, bool((options.browser_profile or {}).get("auto_accept_cookie_consent"))
            )

            # Extract with challenge waiting
            html, text, classification, challenge_resolved = await self._extract(page, options)

            extraction_method = "dom_text"
            final_url = NormalizedUrl(page.url, policy=policy)
            # Re-validate the post-navigation URL: the site may have redirected to an internal
            # host. Fail closed (already admitted as a NormalizedUrl above).
            if self._looks_like(final_url.raw) and not (text or "").strip():
                # Blocking file read + parse: keep it off the event loop like every
                # other document-extraction path.
                text = await asyncio.to_thread(self._pdf_text_extractor, final_url)
                html = html if options.include_html else None
                title = await page.evaluate("() => document.title")
                classification = classify_page(html, text, title=title)
                extraction_method = "pdf_text"

            status = self._status_for(classification)

            metadata: dict[str, Any] = {
                "title": await page.evaluate("() => document.title"),
                "content_length": len(html or ""),
                "browser_reused": self._driver.reused,
                "challenge_resolved": challenge_resolved,
                "extraction_method": extraction_method,
                "intercepted_xhr": intercepted_xhr,
            }

            # Screenshots: optional full-page, plus targeted element screenshots by CSS selector.
            if options.browser_profile.get("include_screenshot"):
                screenshot_bytes = await page.screenshot(full_page=True)
                metadata["screenshot"] = base64.b64encode(screenshot_bytes).decode("ascii")
            element_shots = (options.browser_profile or {}).get("screenshot_selectors") or []
            if element_shots:
                metadata["element_screenshots"] = await self._driver.capture_element_screenshots(
                    page, element_shots
                )

            return CrawlResult(
                url=url,
                attempt=terminal_attempt(
                    strategy=None,
                    status=status,
                    classification=classification,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=None,
                ),
                final_url=final_url,
                html=html,
                text=text,
                attempts=1,
                domain=url.site,
                metadata=metadata,
            )
        finally:
            # Always release the page between crawls (the Camoufox memory-growth
            # root); a wedged context.cookies() falls back cookieless after a bounded wait.
            try:
                await asyncio.wait_for(
                    _harvest_session_cookies(page, url),
                    timeout=self._driver.harvest_timeout_for(options.browser_profile or {}),
                )
            except TimeoutError:
                pass
            await self._driver.safe_close_page(page)
            # Increment render count for ALL attempts (success or failure) so
            # browser recycle logic bounds total attempts per browser instance.
            self._driver.note_render()

    async def extract_with_challenge_wait(
        self, page: Any, options: RenderOptions
    ) -> tuple[str | None, str | None, Classification, bool]:
        html = await self.extract_html(page) if options.include_html else None
        text = await self.extract_text(page) if options.include_text else None
        title = await page.evaluate("() => document.title")
        classification = classify_page(html, text, title=title)

        if classification != "bot_challenge_detected":
            return html, text, classification, False

        deadline = time.monotonic() + self._challenge_wait_seconds
        while time.monotonic() < deadline:
            # Use asyncio.sleep to avoid busy-waiting if injected sleep is patched/no-op
            await asyncio.sleep(1)
            await self._driver.wait_for_ready_state(page)
            next_html = await self.extract_html(page) if options.include_html else None
            next_text = await self.extract_text(page) if options.include_text else None
            next_title = await page.evaluate("() => document.title")
            next_classification = classify_page(next_html, next_text, title=next_title)
            if next_classification != "bot_challenge_detected":
                return next_html, next_text, next_classification, True
            html, text, classification = next_html, next_text, next_classification

        return html, text, classification, False

    @staticmethod
    async def extract_html(page: Any) -> str:
        """The page's serialized HTML, or empty string if the page will not say."""
        try:
            return await page.content()
        except Exception:
            return ""

    @staticmethod
    async def extract_text(page: Any) -> str:
        """The rendered visible text, or empty string if the page will not say.

        Static and public for the same reason ``extract_html`` is: neither reads
        renderer state, so a test that substitutes the extraction *step* needs
        these without carrying a renderer around to call them on.
        """
        try:
            return await page.evaluate("() => document.body ? document.body.innerText: ''")
        except Exception:
            return ""

    def _failure_result(
        self,
        url: NormalizedUrl,
        status: ResultStatus,
        classification: Classification,
        attempts: int,
        started: float,
        error: CrawlError | None,
    ) -> CrawlResult:
        return terminal_crawl_result(
            url=url,
            status=status,
            classification=classification,
            attempts=attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            domain=url.site,
            error=error,
        )

    def _warmup_urls_from_env(self) -> list[str]:
        value = os.environ.get("CRAWLUTRON_WARMUP_URLS", "")
        return [url.strip() for url in value.split(",") if url.strip()]

    def _looks_like_pdf(self, url: str) -> bool:
        return url.split("?", 1)[0].lower().endswith(".pdf")
