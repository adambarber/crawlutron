from __future__ import annotations

import time
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


class BrowserRenderer:
    def __init__(
        self,
        *,
        driver_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._driver_factory = driver_factory or self._undetected_chrome_driver
        self._sleep = sleep

    def render(self, url: str, options: RenderOptions) -> CrawlResult:
        started = time.monotonic()
        attempts = 0
        last_error = None
        max_attempts = max(1, options.max_retries + 1)

        while attempts < max_attempts:
            attempts += 1
            driver = None
            try:
                driver = self._driver_factory()
                driver.set_page_load_timeout(options.timeout_seconds)
                driver.get(url)
                self._wait_for_ready_state(driver)
                html = self._extract_html(driver) if options.include_html else None
                text = self._extract_text(driver) if options.include_text else None
                classification = classify_rendered_page(html, text)
                status = "success" if classification == "rendered" else "blocked"
                return CrawlResult(
                    url=url,
                    status=status,
                    classification=classification,  # type: ignore[arg-type]
                    final_url=getattr(driver, "current_url", url),
                    html=html,
                    text=text,
                    attempts=attempts,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    domain=registrable_domain(url),
                    metadata={
                        "title": getattr(driver, "title", ""),
                        "content_length": len(html or ""),
                        "browser_reused": False,
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

    def _undetected_chrome_driver(self) -> Any:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1365,768")
        return uc.Chrome(options=options)
