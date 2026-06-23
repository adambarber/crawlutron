from __future__ import annotations

import os
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
        driver_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        challenge_wait_seconds: float | None = None,
        pdf_text_extractor: Callable[[str], str] | None = None,
        warmup_urls: list[str] | None = None,
    ) -> None:
        self._driver_factory = driver_factory or self._undetected_chrome_driver
        self._sleep = sleep
        self._challenge_wait_seconds = (
            float(os.environ.get("BROWSER_CRAWLER_CHALLENGE_WAIT_SECONDS", "15"))
            if challenge_wait_seconds is None
            else challenge_wait_seconds
        )
        self._pdf_text_extractor = pdf_text_extractor or self._extract_pdf_text
        self._warmup_urls = warmup_urls if warmup_urls is not None else self._warmup_urls_from_env()

    def render(self, url: str, options: RenderOptions) -> CrawlResult:
        started = time.monotonic()
        attempts = 0
        last_error = None
        max_attempts = max(1, options.max_retries + 1)

        while attempts < max_attempts:
            attempts += 1
            driver = None
            try:
                driver = self._new_driver(options.browser_profile)
                driver.set_page_load_timeout(options.timeout_seconds)
                self._warmup_session(driver)
                driver.get(url)
                self._wait_for_ready_state(driver)
                html, text, classification, challenge_resolved = self._extract_with_challenge_wait(driver, options)
                extraction_method = "dom_text"
                final_url = getattr(driver, "current_url", url)
                if self._looks_like_pdf(final_url) and not (text or "").strip():
                    text = self._pdf_text_extractor(final_url)
                    html = html if options.include_html else None
                    classification = classify_rendered_page(html, text, title=getattr(driver, "title", ""))
                    extraction_method = "pdf_text"
                status = self._status_for_classification(classification)
                return CrawlResult(
                    url=url,
                    status=status,
                    classification=classification,  # type: ignore[arg-type]
                    final_url=final_url,
                    html=html,
                    text=text,
                    attempts=attempts,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    domain=registrable_domain(url),
                    metadata={
                        "title": getattr(driver, "title", ""),
                        "content_length": len(html or ""),
                        "browser_reused": False,
                        "challenge_resolved": challenge_resolved,
                        "extraction_method": extraction_method,
                    },
                )
            except TimeoutError as error:
                last_error = str(error)
                if attempts >= max_attempts:
                    return self._failure_result(url, "retry_exhausted", "navigation_timeout", attempts, started, last_error)
                self._sleep(min(2 ** (attempts - 1), 5))
            except Exception as error:  # noqa: BLE001 - browser drivers raise mixed exception types
                last_error = str(error)
                if attempts >= max_attempts:
                    return self._failure_result(url, "retry_exhausted", "unknown_error", attempts, started, last_error)
                self._sleep(min(2 ** (attempts - 1), 5))
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass

        return self._failure_result(url, "failed", "unknown_error", attempts, started, last_error)

    def _warmup_session(self, driver: Any) -> None:
        for warmup_url in self._warmup_urls:
            driver.get(warmup_url)
            self._wait_for_ready_state(driver)

    def _extract_with_challenge_wait(self, driver: Any, options: RenderOptions) -> tuple[str | None, str | None, str, bool]:
        html = self._extract_html(driver) if options.include_html else None
        text = self._extract_text(driver) if options.include_text else None
        classification = classify_rendered_page(html, text, title=getattr(driver, "title", ""))
        if classification != "bot_challenge_detected":
            return html, text, classification, False

        deadline = time.monotonic() + self._challenge_wait_seconds
        while time.monotonic() < deadline:
            self._sleep(1)
            self._wait_for_ready_state(driver)
            next_html = self._extract_html(driver) if options.include_html else None
            next_text = self._extract_text(driver) if options.include_text else None
            next_classification = classify_rendered_page(next_html, next_text, title=getattr(driver, "title", ""))
            if next_classification != "bot_challenge_detected":
                return next_html, next_text, next_classification, True
            html, text, classification = next_html, next_text, next_classification
        return html, text, classification, False

    def _wait_for_ready_state(self, driver: Any) -> None:
        for _ in range(10):
            state = driver.execute_script("return document.readyState")
            if state in {"interactive", "complete"}:
                return
            self._sleep(0.1)

    def _extract_html(self, driver: Any) -> str:
        try:
            return str(driver.execute_script("return document.documentElement.outerHTML"))
        except Exception:
            return str(getattr(driver, "page_source", ""))

    def _extract_text(self, driver: Any) -> str:
        try:
            return str(driver.execute_script("return document.body ? document.body.innerText : ''"))
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

    def _new_driver(self, browser_profile: dict[str, Any]) -> Any:
        try:
            return self._driver_factory(browser_profile)
        except TypeError:
            return self._driver_factory()

    def _undetected_chrome_driver(self, browser_profile: dict[str, Any] | None = None) -> Any:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        chrome_binary = os.environ.get("BROWSER_CRAWLER_CHROME_BINARY", "/usr/bin/chromium")
        if chrome_binary:
            options.binary_location = chrome_binary
        if self._headless_enabled():
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        profile = browser_profile or {}
        if profile.get("viewport") == "mobile":
            options.add_argument("--window-size=390,844")
            options.add_argument("--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        else:
            options.add_argument("--window-size=1365,768")
        if profile.get("resource_mode") == "reduced":
            options.add_argument("--blink-settings=imagesEnabled=false")

        kwargs: dict[str, Any] = {"options": options}
        driver_path = os.environ.get("BROWSER_CRAWLER_CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
        if driver_path:
            kwargs["driver_executable_path"] = driver_path
        version_main = os.environ.get("BROWSER_CRAWLER_CHROME_VERSION_MAIN", "149")
        if version_main:
            kwargs["version_main"] = int(version_main)
        return uc.Chrome(**kwargs)

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
