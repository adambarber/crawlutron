"""Browser rendering: a headless browser for JavaScript-heavy pages."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import TYPE_CHECKING

from crawlutron.config.env import get_env_float, get_env_int
from crawlutron.config.settings import CrawlerSettings
from crawlutron.extraction.classify import classify_page
from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.result_factory import terminal_crawl_result
from crawlutron.transport.browser_cookies import (
    BROWSER_COOKIES_MAX,
    get_browser_cookies_for,
    store_browser_cookies,
)
from crawlutron.transport.browser_driver import BrowserDriver
from crawlutron.transport.browser_render_phases import (
    ReleasePlan,
    RenderCollaborators,
    WireGuard,
    guarded_render,
    release_page,
)
from crawlutron.transport.browser_targets import (
    make_response_handler,
    make_ssrf_route_handler,
    make_ssrf_websocket_handler,
    resolve_browser_target,
)
from crawlutron.transport.classification import classify_error_code
from crawlutron.transport.driver_config import async_sleep
from crawlutron.transport.pinned_dns import ResolverBudgetExceeded
from crawlutron.transport.render_page import RenderPage

if TYPE_CHECKING:
    from crawlutron.models.host_policy import HostPolicy
    from crawlutron.models.render_options import RenderOptions
    from crawlutron.models.taxonomy import Classification, CrawlError

__all__ = [
    "BrowserRenderer",
    "BROWSER_COOKIES_MAX",
    "store_browser_cookies",
    "get_browser_cookies_for",
    "make_response_handler",
    "make_ssrf_route_handler",
    "make_ssrf_websocket_handler",
    "looks_like_pdf",
    "resolve_browser_target",
]
from crawlutron.transport.document import default_document_text_extractor
from crawlutron.transport.pinned_transport import (
    PinnedRequest,
    build_pinned_request,
    build_pinned_request_async,
)
from crawlutron.vocabulary.browser import BrowserAction
from crawlutron.vocabulary.limits import DEFAULT_CHALLENGE_WAIT_SECONDS, DEFAULT_MAX_CONCURRENT_PAGES
from crawlutron.vocabulary.taxonomy import ResultStatus, status_for_classification

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


def _render_classification(error: BaseException) -> Classification:
    """The attempt verdict for a failed render. A bounded-resolver timeout is a
    resolution failure (dns_error), never a navigation one: the request
    never got to navigate."""
    if isinstance(error, ResolverBudgetExceeded):
        return "dns_error"
    if isinstance(error, TimeoutError):
        return "navigation_timeout"
    return "unknown_error"


def _render_failure_status(classification: Classification) -> ResultStatus:
    """The failure result's status, agreeing with the classification table: the
    loop's own verdict (retry_exhausted) belongs to navigation_timeout and the
    untriaged unknown_error only."""
    if classification == "unknown_error":
        return "retry_exhausted"
    return status_for_classification(classification)


def looks_like_pdf(url: str) -> bool:
    """Whether ``url`` (query stripped) is a PDF by path suffix.
    The is-pdf verdict is reeled into the render pipeline's no-text
    decision, so it lives as a reusable predicate, not a class secret."""
    return url.split("?", 1)[0].lower().endswith(".pdf")


class BrowserRenderer:
    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        challenge_wait_seconds: float | None = None,
        pdf_text_extractor: Callable[[NormalizedUrl], str] | None = None,
        extract: Callable[
            [RenderPage, RenderOptions], Awaitable[tuple[str | None, str | None, Classification, bool]]
        ]
        | None = None,
        looks_like_pdf: Callable[[str], bool] | None = None,
        warmup_urls: list[str] | None = None,
        settings: CrawlerSettings | None = None,
        driver: BrowserDriver | None = None,
        pin: Callable[..., Awaitable[PinnedRequest]] | None = None,
        pin_sync: Callable[..., PinnedRequest] | None = None,
        status_for: Callable[[str], ResultStatus] | None = None,
    ) -> None:
        self._sleep = sleep
        # Policy limit, then the settings field, then env, then the canonical
        # default: the number itself lives only in crawlutron.vocabulary.limits.
        if challenge_wait_seconds is not None:
            self._challenge_wait_seconds = challenge_wait_seconds
        elif settings is not None:
            self._challenge_wait_seconds = float(settings.challenge_wait_seconds)
        else:
            self._challenge_wait_seconds = get_env_float(
                "CRAWLUTRON_CHALLENGE_WAIT_SECONDS", DEFAULT_CHALLENGE_WAIT_SECONDS, lo=0.0, hi=300.0
            )
        self._pdf_text_extractor = pdf_text_extractor or default_document_text_extractor
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
        driver = driver if driver is not None else BrowserDriver(sleep=sleep, settings=settings)
        # Cap concurrent page renders: N tabs at once, or burst load OOMs the browser.
        pages = (
            settings.max_concurrent_pages
            if settings is not None
            else get_env_int("CRAWLUTRON_MAX_CONCURRENT_PAGES", DEFAULT_MAX_CONCURRENT_PAGES, lo=1)
        )
        self._driver = driver
        self._page_semaphore = asyncio.Semaphore(max(1, pages))

    async def _async_sleep(self, seconds: float) -> None:
        """Non-blocking sleep for use inside coroutines: the injectable clock, run
        in a thread, so one render (or the driver between steps) can never freeze
        the event loop.
        """
        await async_sleep(self._sleep, seconds)

    @property
    def pdf_text_extractor(self) -> Callable[[NormalizedUrl], str]:
        """The collaborator that turns a PDF URL into text; public as an injection
        point so a test or caller does not read a private attribute to name the
        extractor in play.
        """
        return self._pdf_text_extractor

    @property
    def driver(self) -> BrowserDriver:
        """The driver this renderer drives the page with; public because it is a
        collaborator, not an implementation detail -- it owns the lifecycle and
        the per-page steps, which tests should name directly.
        """
        return self._driver

    @property
    def warmup_urls(self) -> list[str]:
        """Resolved warmup URLs for this renderer's browser profile: the
        headed-warmed profile carries them, and exposing the resolved list lets
        the pool label renders with the real mode, not an invented one.
        """
        return self._warmup_urls

    async def render_once(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        """Perform exactly one render attempt, concurrency-bound.
        The retrying entry (:meth:`render`) composes this; a caller that
        owns its own retry policy calls it directly."""
        async with self._page_semaphore:
            return await self._render_with_page_unlocked(url, options, policy=policy)

    async def render(
        self,
        url: NormalizedUrl,
        options: RenderOptions,
        *,
        policy: HostPolicy,
    ) -> CrawlResult:
        started, attempts, last_error = time.monotonic(), 0, None
        max_attempts = max(1, options.max_retries + 1)

        while attempts < max_attempts:
            attempts += 1
            try:
                result = await self.render_once(url, options, policy=policy)
                return result
            except Exception as error:
                classification: Classification = _render_classification(error)
                last_error = classify_error_code(error)
                if attempts >= max_attempts:
                    status = _render_failure_status(classification)
                    return self._failure_result(url, status, classification, attempts, started, last_error)
                await self._async_sleep(min(2 ** (attempts - 1), 5))

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    async def close(self) -> None:
        """Release the reused browser instance at service shutdown."""
        await self._driver.close()

    async def _navigate_and_settle(self, page: RenderPage, pinned_url: str) -> None:
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
        pinned_url, goto_headers = await resolve_browser_target(
            url, pin=self._pin, timeout_seconds=options.timeout_seconds
        )

        started = time.monotonic()

        page = await self._driver.open_page(options.browser_profile)
        # The guarded body starts here, not after route wiring: an early raise
        # still recycles via the finally's note_render.
        try:
            result = await guarded_render(
                page,
                url,
                options,
                pinned_url=pinned_url,
                goto_headers=goto_headers,
                started=started,
                collaborators=RenderCollaborators(
                    policy=policy,
                    wire=WireGuard(
                        pin_sync=self._pin_sync, policy=policy, timeout_seconds=options.timeout_seconds
                    ),
                    warmup_urls=self._warmup_urls,
                    driver=self._driver,
                    navigate_and_settle=lambda pinned: self._navigate_and_settle(page, pinned),
                    run_actions=self._run_actions,
                    extract=self._extract,
                    looks_like=self._looks_like,
                    pdf_extractor=self._pdf_text_extractor,
                    status_for=self._status_for,
                ),
            )
        finally:
            # Always release the page between crawls (the Camoufox memory-growth
            # root); a wedged context.cookies() falls back cookieless after a bounded wait.
            await release_page(
                ReleasePlan(page=page, url=url, profile=options.browser_profile or {}),
                harvest_timeout=self._driver.harvest_timeout_for(options.browser_profile or {}),
                close_page=self._driver.safe_close_page,
                note_render=self._driver.note_render,
            )
        return result

    def _run_actions(
        self, page: RenderPage, actions: list[BrowserAction], url: str, budget_seconds: float
    ) -> Awaitable[None]:
        """The policy-specified interaction chain, under its time budget."""
        return self._driver.run_action_chain(page, actions, url=url, budget_seconds=budget_seconds)

    async def extract_with_challenge_wait(
        self, page: RenderPage, options: RenderOptions
    ) -> tuple[str | None, str | None, Classification, bool]:
        html = await self.extract_html(page) if options.include_html else None
        text = await self.extract_text(page) if options.include_text else None
        raw_title = await page.evaluate("() => document.title")
        title = raw_title if isinstance(raw_title, str) else None
        classification = classify_page(html, text, title=title)

        if classification != "bot_challenge_detected":
            return html, text, classification, False

        deadline = time.monotonic() + self._challenge_wait_seconds
        settled = False
        while time.monotonic() < deadline:
            # Poll readiness through the injected clock: one cheap readyState
            # read per tick, no full DOM re-serialization, and the clock is
            # controllable in tests. Never raw asyncio.sleep.
            await self._async_sleep(1)
            ready_state = await page.evaluate("() => document.readyState")
            if ready_state in {"interactive", "complete"}:
                settled = True
                break

        if settled:
            # One final extract to confirm whether the challenge cleared.
            next_html = await self.extract_html(page) if options.include_html else None
            next_text = await self.extract_text(page) if options.include_text else None
            raw_next_title = await page.evaluate("() => document.title")
            next_title = raw_next_title if isinstance(raw_next_title, str) else None
            next_classification = classify_page(next_html, next_text, title=next_title)
            if next_classification != "bot_challenge_detected":
                return next_html, next_text, next_classification, True
            html, text, classification = next_html, next_text, next_classification

        return html, text, classification, False

    @staticmethod
    async def extract_html(page: RenderPage) -> str:
        """The page's serialized HTML, or empty string if the page will not say."""
        try:
            return await page.content()
        except Exception:
            return ""

    @staticmethod
    async def extract_text(page: RenderPage) -> str:
        """The rendered visible text, or empty string if the page will not say --
        static and public for the same reason ``extract_html`` is: neither reads
        renderer state, so a test substituting the step needs these stand-alone.
        """
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText: ''")
        except Exception:
            return ""
        return text if isinstance(text, str) else ""

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
        return looks_like_pdf(url)
