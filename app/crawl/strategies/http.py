"""HTTP fetch strategy: direct + curl_cffi impersonation, conditional caching."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import httpx
import urllib.error
import urllib.request

from app.classify import classify_rendered_page_with_evidence
from app.constants import (
    DEFAULT_EXTRACTION,
    EXTRACTION_DOCUMENT,
    EXTRACTION_FULL,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_DOM_TEXT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
)
from app.crawl.cache import HttpCache
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.extraction import (
    _detect_pagination,
    _extract_discovered_links,
    _extract_document,
    _extract_html,
    _html_diagnostics,
    extract_js_endpoints,
    extract_content,
)
from app.models import CrawlStrategyAttempt, HttpFetchResult, UniversalCrawlResult
from app.validation import normalize_public_http_url, registrable_domain
from urllib.parse import urljoin, urlparse


HttpFetcher = Callable[[str, dict[str, str], int], Any]


# Shared HTTP/2 connection-pooled async client (one per running event loop).
_DEFAULT_HTTP_CLIENT: "httpx.AsyncClient | None" = None


def _build_async_client() -> "httpx.AsyncClient":
    client = httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=30.0,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60.0),
        headers={"User-Agent": "dev-stack-browser-crawler/0.2 (+https://local.dev-stack/crawler)"},
    )
    try:
        client._bound_loop = asyncio.get_event_loop()  # type: ignore[attr-defined]
    except RuntimeError:
        client._bound_loop = None  # type: ignore[attr-defined]
    return client


def get_async_http_client() -> "httpx.AsyncClient":
    """Return the pooled ``httpx.AsyncClient`` bound to the current event loop.

    The client is reused while the running event loop is unchanged (HTTP/2
    connection pooling), and transparently rebuilt if the loop changes (for
    example when a test harness drives many ``asyncio.run`` calls in sequence).
    """
    global _DEFAULT_HTTP_CLIENT
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if _DEFAULT_HTTP_CLIENT is None or _DEFAULT_HTTP_CLIENT.is_closed:
        _DEFAULT_HTTP_CLIENT = _build_async_client()
    elif getattr(_DEFAULT_HTTP_CLIENT, "_bound_loop", None) is not loop:
        _DEFAULT_HTTP_CLIENT = _build_async_client()
    return _DEFAULT_HTTP_CLIENT


async def close_async_http_client() -> None:
    """Close the shared async client on service shutdown."""
    global _DEFAULT_HTTP_CLIENT
    client = _DEFAULT_HTTP_CLIENT
    _DEFAULT_HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


SUCCESS_CLASSIFICATIONS = {"rendered"}
BLOCKED_CLASSIFICATIONS = {
    "captcha_detected",
    "bot_challenge_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
    "cookie_consent_required",
    "javascript_required",
    "unsupported_browser",
    "geolocation_blocked",
    "waf_challenge_detected",
}


async def _default_http_fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
    """Async HTTP/2 fetch using the shared pooled ``httpx.AsyncClient``."""
    client = get_async_http_client()
    started = time.monotonic()
    try:
        response = await client.request(
            "GET", url, headers=headers, timeout=float(timeout_seconds), follow_redirects=True
        )
    except httpx.HTTPStatusError as error:  # 4xx/5xx still return a response
        response = error.response
    elapsed_ms = int((time.monotonic() - started) * 1000)
    body = response.content
    status_code = response.status_code
    # urllib-based callers expect HttpFetchResult even for error statuses.
    return HttpFetchResult(
        url=url,
        final_url=str(response.url),
        status_code=status_code,
        headers={key.lower(): value for key, value in response.headers.items()},
        body=body,
        elapsed_ms=elapsed_ms,
    )


async def _default_cffi_fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
    """TLS-fingerprint-impersonated async HTTP fetcher using curl_cffi."""
    try:
        import curl_cffi.requests as cffi_requests
    except ImportError:
        return await _default_http_fetcher(url, headers, timeout_seconds)
    try:
        async with cffi_requests.AsyncClient(impersonate="chrome124", timeout=float(timeout_seconds)) as client:
            response = await client.get(url, headers=headers, allow_redirects=True)
        elapsed_ms = int(getattr(response, "elapsed", 0) * 1000) if hasattr(response, "elapsed") else 0
        return HttpFetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=response.content,
            elapsed_ms=elapsed_ms,
        )
    except Exception:  # any cffi failure falls back to plain httpx
        return await _default_http_fetcher(url, headers, timeout_seconds)


def _cookie_header_for_url(profile: dict[str, Any], url: str) -> str:
    if profile.get("cookie_jar") not in {"per_host", "session"}:
        return ""
    cookies = profile.get("cookies") or {}
    if not isinstance(cookies, dict):
        return ""
    host = (urlparse(url).hostname or "").lower()
    host_cookies = cookies.get(host) or {}
    if not isinstance(host_cookies, dict):
        return ""
    return "; ".join(f"{key}={value}" for key, value in host_cookies.items())


def _headers_for_strategy(strategy: str, policy: dict[str, Any], url: str) -> dict[str, str]:
    profile = policy.get("request_profile") or {}
    headers = {
        "User-Agent": "dev-stack-browser-crawler/0.2 (+https://local.dev-stack/crawler)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    user_agent_family = str(profile.get("user_agent_family", "generic_crawler"))
    is_browser_like = strategy == HTTP_CFFI_IMPERSONATE or user_agent_family == "desktop_chrome"
    if is_browser_like:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        headers["Referer"] = "https://www.google.com/"
        headers["Sec-Fetch-Site"] = "cross-site"
        headers["Sec-CH-UA"] = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"Windows"'
        headers["Sec-Fetch-User"] = "?1"
    elif user_agent_family == "mobile_safari":
        headers["User-Agent"] = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
    cookie_header = _cookie_header_for_url(profile, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    headers.update(policy["headers"])
    return headers


def _decode_body(body: bytes, content_type: str | None, headers: dict[str, str] | None = None) -> str:
    if headers:
        body = _decompress_body(body, headers.get("content-encoding", ""))
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip()
    return body.decode(charset, errors="replace")


def _status_for_classification(classification: str) -> str:
    if classification in SUCCESS_CLASSIFICATIONS:
        return "success"
    if classification in BLOCKED_CLASSIFICATIONS:
        return "blocked"
    if classification == "not_modified":
        return "success"
    return "failed"


def _quality_score(status: str, classification: str, text: str | None, html: str | None, http_status: int | None) -> float:
    if status != "success" or classification != "rendered":
        return 0.0
    text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    normalized_html = (html or "").lower()
    has_frameset = "<frameset" in normalized_html or "<frame" in normalized_html
    score = 0.45
    if http_status and 200 <= http_status < 300:
        score += 0.2
    if text_length >= 20:
        score += 0.25
    elif text_length > 0:
        score += 0.15
    if html_length >= 40:
        score += 0.05
    if has_frameset and text_length < 40:
        score = min(score, 0.4)
    return min(score, 1.0)


def _content_metrics(text: str | None, html: str | None) -> dict[str, int | float | bool]:
    visible_text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    link_count = len(re.findall(r"<a\b", html or "", flags=re.IGNORECASE))
    frame_count = len(re.findall(r"<iframe\b|<frame\b", html or "", flags=re.IGNORECASE))
    form_count = len(re.findall(r"<form\b", html or "", flags=re.IGNORECASE))
    return {
        "visible_text_length": visible_text_length,
        "html_length": html_length,
        "link_count": link_count,
        "frame_count": frame_count,
        "form_count": form_count,
        "has_frameset": bool(re.search(r"<frameset\b", html or "", flags=re.IGNORECASE)),
        "text_to_html_ratio": round(visible_text_length / html_length, 4) if html_length else 0.0,
    }


def _retry_after_seconds(headers: dict[str, str]) -> int | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return None


async def _fetch_with_retries(
    fetcher: Any,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_retries: int,
) -> tuple[Any, dict[str, Any]]:
    transient_errors: list[str] = []
    max_attempts = max(1, max_retries + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            response = fetcher(url, headers, timeout_seconds)
            if inspect.iscoroutine(response):
                response = await response
            return response, {"attempts": attempt, "transient_errors": transient_errors}
        except Exception as error:  # noqa: BLE001
            transient_errors.append(str(error))
            if attempt >= max_attempts:
                raise


def _looks_like_binary_body(body: bytes, content_type: str | None, url: str) -> bool:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("text/") or normalized_content_type in {"application/json", "application/xml", "application/xhtml+xml"}:
        return False
    if "html" in normalized_content_type or "pdf" in normalized_content_type:
        return False
    lower_url = url.split("?", 1)[0].lower()
    if lower_url.endswith((".zip", ".gz", ".tgz", ".7z", ".rar", ".xlsx", ".docx")):
        return True
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b")):
        return True
    return normalized_content_type in {"application/octet-stream", "application/zip", "application/x-zip-compressed"}


def _binary_no_text_result(
    *,
    original_url: str,
    normalized_url: str,
    final_url: str | None,
    strategy: str,
    http_status: int | None,
    headers: dict[str, str],
    content_type: str | None,
    elapsed_ms: int,
) -> UniversalCrawlResult:
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status="failed",
        classification=EXTRACTION_METHOD_BINARY_NO_TEXT,
        http_status=http_status,
        content_type=content_type,
        elapsed_ms=elapsed_ms,
        error=EXTRACTION_METHOD_BINARY_NO_TEXT,
    )
    return UniversalCrawlResult(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status="failed",
        classification=EXTRACTION_METHOD_BINARY_NO_TEXT,
        selected_strategy=None,
        attempted_strategies=[attempt],
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        html=None,
        text=None,
        extraction_method=EXTRACTION_METHOD_BINARY_NO_TEXT,
        quality_score=0.0,
        error=EXTRACTION_METHOD_BINARY_NO_TEXT,
    )


async def _discovery_diagnostics(
    *,
    normalized_url: str,
    html: str | None,
    policy: dict[str, Any],
    headers: dict[str, str],
    http_fetcher: Any,
) -> dict[str, Any]:
    discovery = policy.get("discovery") or {}
    diagnostics: dict[str, Any] = {}
    parsed = urlparse(normalized_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_sitemaps: list[str] = []
    if discovery.get("robots"):
        robots_url = urljoin(origin, "/robots.txt")
        try:
            robots_response = http_fetcher(robots_url, headers, policy["limits"]["timeout_seconds"])
            if inspect.iscoroutine(robots_response):
                robots_response = await robots_response
            robots_headers = {key.lower(): value for key, value in robots_response.headers.items()}
            robots_text = _decode_body(_decompress_body(robots_response.body, robots_headers.get("content-encoding", "")), robots_headers.get("content-type"))
            crawl_delay = None
            for line in robots_text.splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "crawl-delay":
                    try:
                        crawl_delay = int(value.strip())
                    except ValueError:
                        crawl_delay = None
                elif key.strip().lower() == "sitemap":
                    robots_sitemaps.append(value.strip())
            diagnostics["robots"] = {
                "url": robots_url,
                "allowed": True,
                "crawl_delay_seconds": crawl_delay,
                "sitemaps": robots_sitemaps,
            }
        except Exception as error:  # noqa: BLE001
            diagnostics["robots"] = {"url": robots_url, "error": str(error), "sitemaps": []}
    sitemap_urls: list[str] = []
    if discovery.get("sitemaps"):
        candidate_sitemaps = robots_sitemaps or ["/sitemap.xml"]
        for sitemap_url in (urljoin(origin, entry) for entry in candidate_sitemaps):
            try:
                sitemap_response = http_fetcher(sitemap_url, headers, policy["limits"]["timeout_seconds"])
                if inspect.iscoroutine(sitemap_response):
                    sitemap_response = await sitemap_response
                sitemap_headers = {key.lower(): value for key, value in sitemap_response.headers.items()}
                sitemap_text = _decode_body(_decompress_body(sitemap_response.body, sitemap_headers.get("content-encoding", "")), sitemap_headers.get("content-type"))
                sitemap_urls.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_text, flags=re.IGNORECASE))
            except Exception:
                continue
    feeds: list[str] = []
    if discovery.get("feeds"):
        for match in re.finditer(r"<link\b[^>]*rel=['\"][^'\"]*alternate[^'\"]*['\"][^>]*>", html or "", flags=re.IGNORECASE):
            tag = match.group(0)
            if "rss" not in tag.lower() and "atom" not in tag.lower():
                continue
            href_match = re.search(r"href=['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
            if href_match:
                feeds.append(urljoin(normalized_url, href_match.group(1)))
    if sitemap_urls or feeds:
        diagnostics["discovery"] = {"sitemap_urls": sitemap_urls, "feeds": feeds}
    return diagnostics


async def _http_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    policy: dict[str, Any],
    http_fetcher: Any,
    cache: HttpCache | None = None,
) -> UniversalCrawlResult:
    started = time.monotonic()
    retry_diagnostics: dict[str, Any] = {"attempts": 0, "transient_errors": []}
    request_headers = _headers_for_strategy(strategy, policy, normalized_url)
    if cache is not None:
        request_headers.update(cache.conditional_headers(normalized_url))
    try:
        response, retry_diagnostics = await _fetch_with_retries(
            http_fetcher,
            normalized_url,
            request_headers,
            policy["limits"]["timeout_seconds"],
            policy["limits"]["max_retries"],
        )

        # HTTP conditional caching: a 304 means the page is unchanged.
        if cache is not None and response.status_code == 304:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempt = CrawlStrategyAttempt(
                strategy=strategy,  # type: ignore[arg-type]
                status="success",
                classification="not_modified",
                http_status=304,
                content_type=None,
                elapsed_ms=elapsed_ms,
                error=None,
            )
            return UniversalCrawlResult(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=response.final_url,
                status="success",
                classification="not_modified",
                selected_strategy=strategy,  # type: ignore[arg-type]
                attempted_strategies=[attempt],
                http_status=304,
                headers=response.headers,
                content_type=None,
                html=None,
                text=None,
                extraction_method=None,
                quality_score=0.0,
                diagnostics={"retry": retry_diagnostics, "cache": "not_modified"},
                error=None,
            )

        if cache is not None:
            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")
            if etag or last_modified:
                cache.store(normalized_url, dict(response.headers))

        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type")
        if policy.get("extraction") != EXTRACTION_DOCUMENT and _looks_like_binary_body(response.body, content_type, response.final_url):
            result = _binary_no_text_result(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=response.final_url,
                strategy=strategy,
                http_status=response.status_code,
                headers=headers,
                content_type=content_type,
                elapsed_ms=response.elapsed_ms,
            )
            return UniversalCrawlResult(**{**asdict(result), "diagnostics": {"retry": retry_diagnostics}})
        decompressed = _decompress_body(response.body, headers.get("content-encoding", ""))
        body_text = _decode_body(decompressed, content_type)
        is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(("<html", "<!doctype html"))
        html = body_text if is_html else None
        extraction = policy.get("extraction", DEFAULT_EXTRACTION)
        if extraction == EXTRACTION_DOCUMENT:
            extracted = _extract_document(decompressed, response.final_url)
            text = extracted.text or ""
            markdown = extracted.markdown
            if (content_type or "").lower().startswith("text/"):
                text = _format_extracted_document_text(text, source_url=response.final_url)
                markdown = text
            extraction_method = EXTRACTION_METHOD_DOCUMENT
        elif extraction == EXTRACTION_TRAFILATURA:
            if is_html:
                extracted = _extract_html(body_text, response.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
                text = extracted.text or ""
                markdown = extracted.markdown
            else:
                text = ""
                markdown = None
            extraction_method = EXTRACTION_METHOD_TRAFILATURA
        else:
            extracted = extract_content(decompressed, content_type, response.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
            text = extracted.text or ""
            markdown = extracted.markdown
            extraction_method = EXTRACTION_METHOD_HTTP_HTML if is_html else EXTRACTION_METHOD_HTTP_BODY
        classification, classification_evidence = classify_rendered_page_with_evidence(html, text)
        if response.status_code == 403 and classification == "rendered":
            classification = "access_denied"
            classification_evidence = [{"classification": "access_denied", "signature": "403", "source": "http_status", "context": "HTTP 403"}]
        elif response.status_code == 404:
            classification = "not_found"
            classification_evidence = [{"classification": "not_found", "signature": "404", "source": "http_status", "context": "HTTP 404"}]
        elif response.status_code == 410:
            classification = "gone"
            classification_evidence = [{"classification": "gone", "signature": "410", "source": "http_status", "context": "HTTP 410"}]
        elif response.status_code == 429:
            classification = "rate_limited"
            classification_evidence = [{"classification": "rate_limited", "signature": "429", "source": "http_status", "context": "HTTP 429"}]
        elif response.status_code >= 500:
            classification = "site_error"
            classification_evidence = [{"classification": "site_error", "signature": str(response.status_code), "source": "http_status", "context": f"HTTP {response.status_code}"}]
        if extraction == EXTRACTION_TRAFILATURA and not is_html and classification == "rendered":
            classification = EXTRACTION_METHOD_BINARY_NO_TEXT
            classification_evidence = [{"classification": EXTRACTION_METHOD_BINARY_NO_TEXT, "signature": "non_html_body", "source": "content_type", "context": content_type or ""}]
        status = _status_for_classification(classification)
        diagnostics: dict[str, Any] = {
            "request_profile": {
                "user_agent_family": str(policy.get("request_profile", {}).get("user_agent_family", "generic_crawler")),
                "cookie_jar": str(policy.get("request_profile", {}).get("cookie_jar", "none")),
            },
            "retry": retry_diagnostics,
            "runtime": {"elapsed_ms": response.elapsed_ms, "final_url": response.final_url},
            "content_metrics": _content_metrics(text, html),
        }
        diagnostics.update(_html_diagnostics(html, response.final_url))
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            diagnostics["cooldown"] = {"retry_after_seconds": retry_after, "scope": "domain"}
        diagnostics.update(
            await _discovery_diagnostics(
                normalized_url=normalized_url,
                html=html,
                policy=policy,
                headers=request_headers,
                http_fetcher=http_fetcher,
            )
        )
        diagnostics["js_endpoints"] = extract_js_endpoints(html, normalized_url)
        next_page_url = _detect_pagination(html, response.final_url)
        if next_page_url:
            diagnostics["next_page_url"] = next_page_url
        quality = _quality_score(status, classification, text, html, response.status_code)
        if status == "success" and diagnostics["content_metrics"]["visible_text_length"] >= 40:
            quality = max(quality, 0.95)
        discovered_links, discovered_link_count, discovered_links_truncated = (
            _extract_discovered_links(html, response.final_url, max_links=policy["limits"]["max_discovered_links"]) if status == "success" else ([], 0, False)
        )
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            http_status=response.status_code,
            content_type=content_type,
            elapsed_ms=response.elapsed_ms,
            error=None if status == "success" else classification,
        )
        return UniversalCrawlResult(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=response.final_url,
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            selected_strategy=strategy if status == "success" else None,  # type: ignore[arg-type]
            attempted_strategies=[attempt],
            http_status=response.status_code,
            headers=headers,
            content_type=content_type,
            html=html,
            text=text,
            markdown=markdown,
            extraction_method=extraction_method,
            quality_score=quality,
            classification_evidence=classification_evidence,
            diagnostics=diagnostics,
            discovered_link_count=discovered_link_count,
            discovered_links_truncated=discovered_links_truncated,
            discovered_links=discovered_links,
            error=None if status == "success" else classification,
            next_page_url=next_page_url,
        )
    except Exception as error:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status="failed",
            classification="network_error",
            http_status=None,
            content_type=None,
            elapsed_ms=elapsed_ms,
            error=str(error),
        )
        return UniversalCrawlResult(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=None,
            status="failed",
            classification="network_error",
            selected_strategy=None,
            attempted_strategies=[attempt],
            http_status=None,
            headers={},
            content_type=None,
            html=None,
            text=None,
            extraction_method=None,
            quality_score=0.0,
            diagnostics={"retry": retry_diagnostics, "runtime": {"elapsed_ms": elapsed_ms}, "request_profile": policy.get("request_profile") or {}},
            error=str(error),
        )


# ---------------------------------------------------------------------------
# Document text annotation (agenda/legal document formatting)
# ---------------------------------------------------------------------------


def _format_extracted_document_text(text: str, *, source_url: str) -> str:
    """Normalize scraper-extracted document text for downstream annotation."""
    lines = [_clean_document_line(line) for line in text.splitlines()]
    records: list[str] = []
    action_by_item: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        if _is_document_non_item_section(line):
            index += 1
            continue

        range_action_match = re.match(r"^(?P<start>\d+[a-z])\s*-\s*(?P<end>\d+[a-z])\s+(?P<action>.+)$", line, flags=re.IGNORECASE)
        if range_action_match:
            action = range_action_match.group("action").strip()
            for item_number in _document_item_range(range_action_match.group("start"), range_action_match.group("end")):
                action_by_item[item_number] = action
            index += 1
            continue

        action_match = re.match(r"^(?P<number>\d+[a-z])\s+(?P<action>.+)$", line, flags=re.IGNORECASE)
        if action_match and not line.endswith("."):
            action_by_item[action_match.group("number").lower()] = action_match.group("action").strip()
            index += 1
            continue

        section_match = re.match(r"^(?P<number>\d+)\.$", line)
        if section_match:
            title_index = _next_non_empty_document_line(lines, index + 1)
            if title_index is not None and not _is_document_item_marker(lines[title_index]):
                records.append(f"{section_match.group('number')}. {lines[title_index]}")
                index = title_index + 1
                continue

        item_match = re.match(r"^(?P<number>\d+[a-z])\.$", line, flags=re.IGNORECASE)
        if item_match:
            number = item_match.group("number").lower()
            title_lines, next_index = _collect_document_item_lines(lines, index + 1)
            if title_lines:
                action = action_by_item.get(number)
                action_suffix = f" [Action: {action}]" if action else ""
                records.append(f"{number}. {' '.join(title_lines)}{action_suffix}")
                index = next_index
                continue

        records.append(line)
        index += 1

    return "\n\n".join(_merge_document_paragraph_records(records)).strip()


def _merge_document_paragraph_records(records: list[str]) -> list[str]:
    merged: list[str] = []
    for record in records:
        if merged and _should_join_document_paragraph(merged[-1], record):
            merged[-1] = _join_document_paragraph_lines(merged[-1], record)
            continue
        merged.append(record)
    return merged


def _should_join_document_paragraph(previous: str, current: str) -> bool:
    if _is_numbered_document_record(previous) or _is_numbered_document_record(current):
        return False
    if _looks_like_document_heading(previous) or _looks_like_document_heading(current):
        return False
    if re.match(r"^[,;:.!?)]", current):
        return True
    if not re.search(r"[.!?:;)]$", previous) and _looks_like_document_prose(previous) and _looks_like_document_prose(current):
        return True
    return _looks_like_document_prose(previous) and _looks_like_document_prose(current) and current[:1].islower()


def _join_document_paragraph_lines(previous: str, current: str) -> str:
    if re.match(r"^[,;:.!?)]", current):
        return f"{previous}{current[0]} {current[1:].lstrip()}"
    return f"{previous} {current}"


def _looks_like_document_prose(record: str) -> bool:
    return bool(re.search(r"[a-z]", record)) and len(record.split()) >= 3


def _looks_like_document_heading(record: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", record)
    return bool(letters) and letters.upper() == letters and len(record.split()) <= 8


def _is_numbered_document_record(record: str) -> bool:
    return bool(re.match(r"^\d+[a-z]?\.\s", record, flags=re.IGNORECASE))


def _clean_document_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\f", " ")).strip()


def _next_non_empty_document_line(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        if lines[index]:
            return index
    return None


def _is_document_item_marker(line: str) -> bool:
    return bool(re.match(r"^\d+[a-z]\.?$", line, flags=re.IGNORECASE))


def _is_document_record_boundary(line: str) -> bool:
    return bool(
        re.match(r"^\d+\.$", line)
        or re.match(r"^\d+[a-z]\.?$", line, flags=re.IGNORECASE)
        or re.match(r"^\d+[a-z]\s+.+$", line, flags=re.IGNORECASE)
        or _is_document_non_item_section(line)
        or line.lower() == "attachments"
    )


def _is_document_non_item_section(line: str) -> bool:
    return line.upper().startswith("PUBLIC PARTICIPATION")


def _collect_document_item_lines(lines: list[str], start: int) -> tuple[list[str], int]:
    title_lines: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        numbered_continuation = re.match(r"^\d+\.\s+(?P<text>.+)$", line)
        if numbered_continuation:
            title_lines.append(numbered_continuation.group("text"))
            index += 1
            continue
        if re.match(r"^\d+\.$", line):
            next_index = _next_non_empty_document_line(lines, index + 1)
            if next_index is not None and not _is_document_non_item_section(lines[next_index]):
                title_lines.append(lines[next_index])
                index = next_index + 1
                continue
            break
        if _is_document_record_boundary(line):
            break
        title_lines.append(line)
        index += 1
    return title_lines, index


def _document_item_range(start: str, end: str) -> list[str]:
    start_match = re.match(r"^(\d+)([a-z])$", start.lower())
    end_match = re.match(r"^(\d+)([a-z])$", end.lower())
    if not start_match or not end_match or start_match.group(1) != end_match.group(1):
        return [start.lower(), end.lower()]
    prefix = start_match.group(1)
    return [f"{prefix}{chr(code)}" for code in range(ord(start_match.group(2)), ord(end_match.group(2)) + 1)]
