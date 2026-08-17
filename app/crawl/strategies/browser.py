"""Browser render strategy (Camoufox/Playwright) with SOTA discovery hooks.

Owns the :class:`BrowserRenderer` which drives a headless/full browser to
render JavaScript-heavy pages. Beyond basic DOM extraction it now also:

* intercepts XHR/fetch JSON responses (Katana-style dynamic endpoint discovery),
* performs lazy-load infinite-scroll to trigger deferred content, and
* optionally captures a full-page screenshot as base64 for debugging.
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable
from typing import Any

from app.classify import classify_rendered_page
from app.crawl.strategies.document import _default_document_text_extractor
from app.models import CrawlResult, RenderOptions
from app.crawl.strategies.http import CookieJar, _check_ssrf_protection
from app.validation import registrable_domain


TRANSIENT_CLASSIFICATIONS = {
    "navigation_timeout",
    "browser_crash",
    "network_error",
    "tls_error",
    "dns_error",
    "unknown_error",
}
BLOCKED_CLASSIFICATIONS = {
    "captcha_detected",
    "bot_challenge_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
}
FAILED_CLASSIFICATIONS = {"not_found", "site_error", "gone", "binary_no_text"}

LAZY_SCROLL_STEPS = 3
LAZY_SCROLL_DELAY_SECONDS = 1


class BrowserRenderer:
    def __init__(
        self,
        *,
        browser_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        challenge_wait_seconds: float | None = None,
        pdf_text_extractor: Callable[[str], str] | None = None,
        warmup_urls: list[str] | None = None,
    ) -> None:
        self._browser_factory = browser_factory or self._create_camoufox_browser
        self._sleep = sleep
        self._challenge_wait_seconds = (
            float(os.environ.get("BROWSER_CRAWLER_CHALLENGE_WAIT_SECONDS", "15"))
            if challenge_wait_seconds is None
            else challenge_wait_seconds
        )
        self._pdf_text_extractor = pdf_text_extractor or _default_document_text_extractor
        self._warmup_urls = warmup_urls if warmup_urls is not None else self._warmup_urls_from_env()
        # Reusable browser instance (#6): one browser per BrowserRenderer lifecycle.
        self._browser_cm: Any = None
        self._browser: Any = None
        self._browser_reused = False
        # Recycle the reused browser after this many successful renders to bound
        # the memory of a long-lived Camoufox process (see _acquire_browser).
        self._render_count = 0
        self._max_browser_renders = int(os.environ.get("BROWSER_CRAWLER_MAX_BROWSER_RENDERS", "100"))

    async def _reset_browser(self) -> None:
        """Close any cached browser instance so the next render recreates it.

        The previous browser context manager is torn down (best-effort) so its
        underlying Camoufox process is not leaked between failed attempts.
        """
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        self._browser_reused = False
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # best-effort teardown
                pass

    async def render(self, url: str, options: RenderOptions) -> CrawlResult:
        started = time.monotonic()
        attempts = 0
        last_error = None
        max_attempts = max(1, options.max_retries + 1)

        while attempts < max_attempts:
            attempts += 1
            try:
                result = await self._render_with_browser(url, options)
                return result
            except TimeoutError as error:
                last_error = str(error)
                await self._reset_browser()
                if attempts >= max_attempts:
                    return self._failure_result(url, "retry_exhausted", "navigation_timeout", attempts, started, last_error)
                self._sleep(min(2 ** (attempts - 1), 5))
            except Exception as error:  # noqa: BLE001
                last_error = str(error)
                await self._reset_browser()
                if attempts >= max_attempts:
                    return self._failure_result(url, "retry_exhausted", "unknown_error", attempts, started, last_error)
                self._sleep(min(2 ** (attempts - 1), 5))

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    async def _acquire_browser(self, options: RenderOptions) -> Any:
        """Return a reused browser instance, launching one on first use (#6).

        Recycles the browser once it has served ``_max_browser_renders`` to
        bound memory growth of a long-lived Camoufox process.
        """
        if (
            self._browser is None
            or getattr(self._browser, "is_closed", False)
            or self._render_count >= self._max_browser_renders
        ):
            if self._browser is not None:
                await self._reset_browser()
            launch_kwargs = self._get_browser_kwargs(options.browser_profile)
            cm = self._browser_factory(**launch_kwargs)
            self._browser = await cm.__aenter__()
            self._browser_cm = cm
            self._browser_reused = False
            self._render_count = 0
        else:
            self._browser_reused = True
        return self._browser

    @staticmethod
    async def _safe_close_page(page: Any) -> None:
        """Best-effort page teardown so rendered tabs/processes are not leaked."""
        if page is None:
            return
        try:
            close = getattr(page, "close", None)
            if close is not None:
                await close()
        except Exception:  # best-effort teardown
            pass

    async def close(self) -> None:
        """Release the reused browser instance at service shutdown (#6)."""
        cm = self._browser_cm
        self._browser_cm = None
        self._browser = None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # best-effort teardown
                pass

    async def _render_with_browser(self, url: str, options: RenderOptions) -> CrawlResult:
        # SSRF protection (#7): validate URL before navigating
        _check_ssrf_protection(url)

        started = time.monotonic()

        browser = await self._acquire_browser(options)
        page = await browser.new_page(**self._get_page_kwargs(options.browser_profile))
        try:
            # Katana-style XHR/fetch interception.
            intercepted_xhr: list[dict[str, Any]] = []
            page.on("response", self._make_response_handler(intercepted_xhr))

            # Warmup URLs
            for warmup_url in self._warmup_urls:
                await page.goto(warmup_url, wait_until="domcontentloaded")
                await self._wait_for_ready_state(page)

            # Navigate to target URL
            await page.goto(url, wait_until="domcontentloaded")
            await self._wait_for_ready_state(page)

            # Browser action chains (#10): run policy-specified interactions before extraction.
            actions = (options.browser_profile or {}).get("actions") or []
            if actions:
                await self._run_action_chain(page, actions)

            # Lazy-load scroll: trigger deferred/infinite-scroll content.
            await self._lazy_load_scroll(page)

            # Extract with challenge waiting
            html, text, classification, challenge_resolved = await self._extract_with_challenge_wait(page, options)

            extraction_method = "dom_text"
            final_url = page.url

            # PDF extraction fallback
            if self._looks_like_pdf(final_url) and not (text or "").strip():
                text = self._pdf_text_extractor(final_url)
                html = html if options.include_html else None
                title = await page.evaluate("() => document.title")
                classification = classify_rendered_page(html, text, title=title)
                extraction_method = "pdf_text"

            status = self._status_for_classification(classification)

            metadata: dict[str, Any] = {
                "title": await page.evaluate("() => document.title"),
                "content_length": len(html or ""),
                "browser_reused": self._browser_reused,
                "challenge_resolved": challenge_resolved,
                "extraction_method": extraction_method,
                "intercepted_xhr": intercepted_xhr,
            }

            # Screenshots (#14): optional full-page, plus targeted element screenshots by CSS selector.
            if options.browser_profile.get("include_screenshot"):
                screenshot_bytes = await page.screenshot(full_page=True)
                metadata["screenshot"] = base64.b64encode(screenshot_bytes).decode("ascii")
            element_shots = (options.browser_profile or {}).get("screenshot_selectors") or []
            if element_shots:
                metadata["element_screenshots"] = await self._capture_element_screenshots(page, element_shots)

            self._render_count += 1
            return CrawlResult(
                url=url,
                status=status,
                classification=classification,  # type: ignore[arg-type]
                final_url=final_url,
                html=html,
                text=text,
                attempts=1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                domain=registrable_domain(url),
                metadata=metadata,
            )
        finally:
            # Always release the page so the rendered tab/process is not leaked
            # between crawls (the root of the Camoufox memory growth).
            await self._safe_close_page(page)

    async def _run_action_chain(self, page: Any, actions: list[dict[str, Any]]) -> None:
        """Execute a sequence of browser actions before extraction (#10).

        Supported action types: ``click``, ``fill``, ``hover``, ``scroll``
        (into an element), ``scroll_bottom``, ``wait`` (for a selector),
        ``wait_seconds``, and ``execute_js``.
        """
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type", "")).lower()
            selector = action.get("selector")
            try:
                if action_type == "click" and selector:
                    await page.click(selector)
                elif action_type == "fill" and selector:
                    await page.fill(selector, str(action.get("value", "")))
                elif action_type == "hover" and selector:
                    await page.hover(selector)
                elif action_type == "scroll" and selector:
                    element = await page.query_selector(selector)
                    if element is not None:
                        await element.scroll_into_view_if_needed()
                elif action_type == "scroll_bottom":
                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                elif action_type == "wait" and selector:
                    await page.wait_for_selector(selector, timeout=int(action.get("timeout", 5000)))
                elif action_type == "wait_seconds":
                    self._sleep(float(action.get("seconds", 1)))
                elif action_type == "execute_js" and "script" in action:
                    await page.evaluate(action["script"])
            except Exception:  # action chains are best-effort
                continue

    async def _capture_element_screenshots(self, page: Any, selectors: list[str]) -> list[dict[str, Any]]:
        """Capture targeted element screenshots as base64 (#14)."""
        shots: list[dict[str, Any]] = []
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element is None:
                    continue
                data = base64.b64encode(await element.screenshot()).decode("ascii")
                shots.append({"selector": selector, "screenshot": data})
            except Exception:  # best-effort per-element capture
                continue
        return shots

    @staticmethod
    def _make_response_handler(collected: list[dict[str, Any]]) -> Callable[[Any], Awaitable[None]]:
        async def on_response(response: Any) -> None:
            headers = response.headers or {}
            content_type = ""
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if str(key).lower() == "content-type":
                        content_type = str(value)
                        break
            if "application/json" not in content_type:
                return
            entry: dict[str, Any] = {
                "url": getattr(response, "url", ""),
                "content_type": content_type,
                "status": getattr(response, "status", None),
                "payload": None,
            }
            try:
                payload = await response.json()
                entry["payload"] = payload
            except Exception:  # payload capture is best-effort
                entry["payload"] = None
            collected.append(entry)

        return on_response

    async def _lazy_load_scroll(self, page: Any) -> None:
        """Scroll to the bottom repeatedly to trigger lazy-loaded content."""
        for _ in range(LAZY_SCROLL_STEPS):
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:  # scrolling is best-effort
                return
            self._sleep(LAZY_SCROLL_DELAY_SECONDS)

    def _get_browser_kwargs(self, browser_profile: dict[str, Any]) -> dict[str, Any]:
        """Launch-only kwargs for the Camoufox/Playwright browser factory.

        Persistent cookies are passed via ``storage_state`` so cookies from a
        previous session are retained across recursive page loads."""
        kwargs: dict[str, Any] = {
            "headless": self._headless_enabled(),
            "humanize": True,
        }

        profile = browser_profile or {}

        if profile.get("resource_mode") == "reduced":
            kwargs["args"] = ["--blink-settings=imagesEnabled=false"]

        storage_state = profile.get("storage_state")
        if storage_state:
            kwargs["storage_state"] = storage_state

        return kwargs

    def _get_page_kwargs(self, browser_profile: dict[str, Any]) -> dict[str, Any]:
        """Page/context kwargs, applied at ``browser.new_page`` time."""
        profile = browser_profile or {}
        viewport = profile.get("viewport")
        if viewport == "mobile":
            return {"viewport": {"width": 390, "height": 844}}
        return {"viewport": {"width": 1365, "height": 768}}

    async def _extract_with_challenge_wait(self, page: Any, options: RenderOptions) -> tuple[str | None, str | None, str, bool]:
        html = await self._extract_html(page) if options.include_html else None
        text = await self._extract_text(page) if options.include_text else None
        title = await page.evaluate("() => document.title")
        classification = classify_rendered_page(html, text, title=title)

        if classification != "bot_challenge_detected":
            return html, text, classification, False

        deadline = time.monotonic() + self._challenge_wait_seconds
        while time.monotonic() < deadline:
            self._sleep(1)
            await self._wait_for_ready_state(page)
            next_html = await self._extract_html(page) if options.include_html else None
            next_text = await self._extract_text(page) if options.include_text else None
            next_title = await page.evaluate("() => document.title")
            next_classification = classify_rendered_page(next_html, next_text, title=next_title)
            if next_classification != "bot_challenge_detected":
                return next_html, next_text, next_classification, True
            html, text, classification = next_html, next_text, next_classification

        return html, text, classification, False

    async def _wait_for_ready_state(self, page: Any) -> None:
        for _ in range(10):
            state = await page.evaluate("() => document.readyState")
            if state in {"interactive", "complete"}:
                return
            self._sleep(0.1)

    async def _extract_html(self, page: Any) -> str:
        try:
            return await page.content()
        except Exception:
            return ""

    async def _extract_text(self, page: Any) -> str:
        try:
            return await page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return ""

    def _failure_result(
        self,
        url: str,
        status: str,
        classification: str,
        attempts: int,
        started: float,
        error: str | None,
    ) -> CrawlResult:
        return CrawlResult(
            url=url,
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            final_url=None,
            html=None,
            text=None,
            attempts=attempts,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            domain=registrable_domain(url),
            error=error,
        )

    def _create_camoufox_browser(self, **kwargs: Any):
        """Factory for AsyncCamoufox browser."""
        from camoufox.async_api import AsyncCamoufox
        return AsyncCamoufox(**kwargs)

    def _warmup_urls_from_env(self) -> list[str]:
        value = os.environ.get("BROWSER_CRAWLER_WARMUP_URLS", "")
        return [url.strip() for url in value.split(",") if url.strip()]

    def _headless_enabled(self) -> bool:
        return os.environ.get("BROWSER_CRAWLER_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}

    def _looks_like_pdf(self, url: str) -> bool:
        return url.split("?", 1)[0].lower().endswith(".pdf")

    def _status_for_classification(self, classification: str) -> str:
        if classification == "rendered":
            return "success"
        if classification in BLOCKED_CLASSIFICATIONS:
            return "blocked"
        if classification in FAILED_CLASSIFICATIONS:
            return "failed"
        return "blocked"
