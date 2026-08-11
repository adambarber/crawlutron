from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from .classify import classify_rendered_page
from .models import CrawlResult, RenderOptions
from .validation import registrable_domain


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
        self._pdf_text_extractor = pdf_text_extractor or self._extract_pdf_text
        self._warmup_urls = warmup_urls if warmup_urls is not None else self._warmup_urls_from_env()

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
                if attempts >= max_attempts:
                    return self._failure_result(url, "retry_exhausted", "navigation_timeout", attempts, started, last_error)
                self._sleep(min(2 ** (attempts - 1), 5))
            except Exception as error:  # noqa: BLE001
                last_error = str(error)
                if attempts >= max_attempts:
                    return self._failure_result(url, "retry_exhausted", "unknown_error", attempts, started, last_error)
                self._sleep(min(2 ** (attempts - 1), 5))

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    async def _render_with_browser(self, url: str, options: RenderOptions) -> CrawlResult:
        started = time.monotonic()

        # Create browser with Camoufox
        browser_kwargs = self._get_browser_kwargs(options.browser_profile)

        async with self._browser_factory(**browser_kwargs) as browser:
            page = await browser.new_page()

            # Warmup URLs
            for warmup_url in self._warmup_urls:
                await page.goto(warmup_url, wait_until="domcontentloaded")
                await self._wait_for_ready_state(page)

            # Navigate to target URL
            await page.goto(url, wait_until="domcontentloaded")
            await self._wait_for_ready_state(page)

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
                metadata={
                    "title": await page.evaluate("() => document.title"),
                    "content_length": len(html or ""),
                    "browser_reused": False,
                    "challenge_resolved": challenge_resolved,
                    "extraction_method": extraction_method,
                },
            )

    def _get_browser_kwargs(self, browser_profile: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": self._headless_enabled(),
            "humanize": True,
        }

        profile = browser_profile or {}

        # Viewport settings
        viewport = profile.get("viewport")
        if viewport == "mobile":
            kwargs["viewport"] = {"width": 390, "height": 844}
        else:
            kwargs["viewport"] = {"width": 1365, "height": 768}

        # Resource mode
        if profile.get("resource_mode") == "reduced":
            kwargs["browser_args"] = ["--blink-settings=imagesEnabled=false"]

        return kwargs

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

    def _extract_pdf_text(self, url: str) -> str:
        with urllib.request.urlopen(url, timeout=30) as response:
            pdf_bytes = response.read()
        completed = subprocess.run(
            ["pdftotext", "-", "-"],
            input=pdf_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.decode("utf-8", errors="replace").strip()

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
