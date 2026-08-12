from __future__ import annotations

import asyncio
import inspect
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from html import unescape
from typing import Any

from .api_models import POLICY_SCHEMA_VERSION
from .classify import classify_rendered_page_with_evidence
from .models import CrawlJob, CrawlResult, CrawlStrategyAttempt, DiscoveredLink, HttpFetchResult, RenderOptions, UniversalCrawlResult
from .scheduler import DomainScheduler
from .settings import CrawlerSettings
from .validation import normalize_public_http_url, registrable_domain


CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult] | CrawlResult]
HttpFetcher = Callable[[str, dict[str, str], int], HttpFetchResult]
DocumentTextExtractor = Callable[[bytes, str | None, str], str]
TrafilaturaExtractor = Callable[[str, str], str]


@dataclass(frozen=True)
class CrawlExecutionJob:
    index: int
    original_url: str
    normalized_url: str
    domain: str

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
HTTP_STRATEGIES = {"HTTP_DECLARED_CRAWLER", "HTTP_SITE_COMPAT", "HTTP_CFFI_IMPERSONATE", "DOCUMENT_EXTRACT", "TRAFILATURA_EXTRACT"}
BROWSER_STRATEGIES = {"BROWSER_HEADLESS", "BROWSER_HEADED_WARMED"}
SUPPORTED_CRAWL_STRATEGIES = HTTP_STRATEGIES | BROWSER_STRATEGIES
DEFAULT_CRAWL_STRATEGIES = ["HTTP_DECLARED_CRAWLER", "HTTP_SITE_COMPAT", "HTTP_CFFI_IMPERSONATE", "BROWSER_HEADLESS"]


def _payload_urls(payload: dict[str, Any]) -> list[str]:
    if "urls" in payload:
        urls = payload["urls"]
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("invalid_urls")
        return urls
    if "url" in payload and isinstance(payload["url"], str):
        return [payload["url"]]
    raise ValueError("missing_url")


def _render_options(payload: dict[str, Any], settings: CrawlerSettings) -> RenderOptions:
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("invalid_options")
    return RenderOptions(
        timeout_seconds=int(options.get("timeout_seconds", settings.default_timeout_seconds)),
        wait_until=str(options.get("wait_until", "network_idle_or_timeout")),
        max_retries=int(options.get("max_retries", settings.default_max_retries)),
        respect_robots=bool(options.get("respect_robots", settings.respect_robots)),
        include_html=bool(options.get("include_html", True)),
        include_text=bool(options.get("include_text", True)),
    )


def _disallowed_result(index_url: tuple[int, str], classification: str, elapsed_ms: int = 0) -> CrawlResult:
    index, url = index_url
    return CrawlResult(
        url=url,
        status="disallowed",
        classification=classification,  # type: ignore[arg-type]
        final_url=None,
        html=None,
        text=None,
        attempts=0,
        elapsed_ms=elapsed_ms,
        domain="",
        error=classification,
    )


async def _call_worker(worker: CrawlWorker, job: CrawlJob) -> CrawlResult:
    result = worker(job)
    if inspect.iscoroutine(result):
        return await result
    return await asyncio.to_thread(worker, job)


def _result_dict(result: CrawlResult) -> dict[str, Any]:
    return asdict(result)


def _universal_result_dict(result: UniversalCrawlResult) -> dict[str, Any]:
    return asdict(result)


def _default_http_fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return HttpFetchResult(
                url=url,
                final_url=response.geturl(),
                status_code=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
    except urllib.error.HTTPError as error:
        return HttpFetchResult(
            url=url,
            final_url=error.geturl(),
            status_code=error.code,
            headers={key.lower(): value for key, value in error.headers.items()},
            body=error.read(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _default_cffi_fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
    """TLS-fingerprint-impersonated HTTP fetcher using curl_cffi."""
    try:
        import curl_cffi.requests as cffi_requests
    except ImportError:
        return _default_http_fetcher(url, headers, timeout_seconds)
    started = time.monotonic()
    try:
        response = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome124",
            timeout=timeout_seconds,
        )
        return HttpFetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=response.content,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as error:  # noqa: BLE001
        raise


def _payload_policy(payload: dict[str, Any], settings: CrawlerSettings) -> dict[str, Any]:
    policy = payload.get("policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("invalid_policy")
    limits = policy.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("invalid_policy_limits")
    headers = policy.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("invalid_policy_headers")
    strategies = policy.get("strategies") or DEFAULT_CRAWL_STRATEGIES
    if not isinstance(strategies, list) or not all(isinstance(strategy, str) for strategy in strategies) or not strategies:
        raise ValueError("invalid_policy_strategies")
    if any(strategy not in SUPPORTED_CRAWL_STRATEGIES for strategy in strategies):
        raise ValueError("invalid_policy_strategy")
    request_profile = policy.get("request_profile") or {}
    browser_profile = policy.get("browser_profile") or {}
    discovery = policy.get("discovery") or {}
    if not isinstance(request_profile, dict) or not isinstance(browser_profile, dict) or not isinstance(discovery, dict):
        raise ValueError("invalid_policy_profile")
    return {
        "schema_version": str(policy.get("schema_version", POLICY_SCHEMA_VERSION)),
        "strategies": strategies,
        "limits": {
            "timeout_seconds": int(limits.get("timeout_seconds", settings.default_timeout_seconds)),
            "max_retries": int(limits.get("max_retries", settings.default_max_retries)),
            "challenge_wait_seconds": int(limits.get("challenge_wait_seconds", 15)),
            "max_discovered_links": int(limits.get("max_discovered_links", 250)),
        },
        "headers": {str(key): str(value) for key, value in headers.items()},
        "request_profile": dict(request_profile),
        "browser_profile": dict(browser_profile),
        "discovery": {str(key): bool(value) for key, value in discovery.items()},
    }


def _headers_for_strategy(strategy: str, policy: dict[str, Any], url: str) -> dict[str, str]:
    profile = policy.get("request_profile") or {}
    # Use gzip, deflate, br instead of identity for Accept-Encoding
    # Accept-Language is always present for all strategies
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
    is_browser_like = strategy == "HTTP_SITE_COMPAT" or user_agent_family == "desktop_chrome"
    if is_browser_like:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        # Add Referer for browser-like strategies
        headers["Referer"] = "https://www.google.com/"
        # Fix Sec-Fetch-Site: cross-site because we have a Google Referer
        headers["Sec-Fetch-Site"] = "cross-site"
        # Add User-Agent Client Hints
        headers["Sec-CH-UA"] = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"Windows"'
        # Sec-Fetch-User indicates a user-triggered navigation
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


# Decompression extracted to app.crawl.decompression
from app.crawl.decompression import decompress_body as _decompress_body


def _decode_body(body: bytes, content_type: str | None, headers: dict[str, str] | None = None) -> str:
    # Decompress if needed
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


def _fetch_with_retries(
    fetcher: HttpFetcher,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_retries: int,
) -> tuple[HttpFetchResult, dict[str, Any]]:
    transient_errors: list[str] = []
    max_attempts = max(1, max_retries + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            response = fetcher(url, headers, timeout_seconds)
            return response, {"attempts": attempt, "transient_errors": transient_errors}
        except Exception as error:  # noqa: BLE001 - callers can inject mixed HTTP errors
            transient_errors.append(str(error))
            if attempt >= max_attempts:
                raise
    raise RuntimeError("unreachable_http_retry")


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a" and self._current is None:
            self._current = {"attrs": {key.lower(): value or "" for key, value in attrs}, "text": []}
            self._depth = 1
        elif self._current is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        self._depth -= 1
        if tag.lower() == "a" or self._depth <= 0:
            self.anchors.append(self._current)
            self._current = None
            self._depth = 0

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)


class _FrameCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.frames: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag not in {"frame", "iframe"}:
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        src = unescape(attr_map.get("src", "")).strip()
        if not src:
            return
        self.frames.append(
            {
                "url": src,
                "name": attr_map.get("name", ""),
                "title": attr_map.get("title", ""),
            }
        )


class _FormCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if normalized_tag == "form":
            self._current = {"attrs": attr_map, "controls": []}
            return
        if self._current is None:
            return
        if normalized_tag == "input":
            control_type = (attr_map.get("type") or "text").lower()
            self._current["controls"].append(
                {
                    "tag": "input",
                    "type": control_type,
                    "name": attr_map.get("name", ""),
                    "id": attr_map.get("id", ""),
                    "value": attr_map.get("value", ""),
                }
            )
        elif normalized_tag in {"select", "textarea", "button"}:
            self._current["controls"].append(
                {
                    "tag": normalized_tag,
                    "type": attr_map.get("type", ""),
                    "name": attr_map.get("name", ""),
                    "id": attr_map.get("id", ""),
                    "value": attr_map.get("value", ""),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _extract_frames(html: str | None, source_url: str | None) -> list[dict[str, Any]]:
    if not html or not source_url:
        return []
    source_host = (urlparse(source_url).hostname or "").lower()
    collector = _FrameCollector()
    collector.feed(html)
    frames: list[dict[str, Any]] = []
    seen: set[str] = set()
    for frame in collector.frames:
        normalized_url = urljoin(source_url, frame["url"])
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        frames.append(
            {
                "url": frame["url"],
                "normalized_url": normalized_url,
                "name": frame["name"],
                "title": frame["title"],
                "source_url": source_url,
                "host": parsed.hostname.lower(),
                "is_same_host": parsed.hostname.lower() == source_host,
            }
        )
    return frames


def _extract_forms(html: str | None, source_url: str | None) -> list[dict[str, Any]]:
    if not html or not source_url:
        return []
    collector = _FormCollector()
    collector.feed(html)
    forms: list[dict[str, Any]] = []
    for form in collector.forms:
        attrs = form["attrs"]
        action = unescape(str(attrs.get("action", "")).strip())
        normalized_action = urljoin(source_url, action) if action else source_url
        forms.append(
            {
                "method": (attrs.get("method") or "GET").upper(),
                "action": action,
                "normalized_action": normalized_action,
                "target": attrs.get("target", ""),
                "name": attrs.get("name", ""),
                "id": attrs.get("id", ""),
                "controls": form["controls"],
            }
        )
    return forms


def _html_diagnostics(html: str | None, source_url: str | None) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    frames = _extract_frames(html, source_url)
    forms = _extract_forms(html, source_url)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics


def _extract_discovered_links(html: str | None, source_url: str | None, *, max_links: int = 250) -> tuple[list[DiscoveredLink], int, bool]:
    if not html or not source_url:
        return [], 0, False
    source_host = (urlparse(source_url).hostname or "").lower()
    collector = _AnchorCollector()
    collector.feed(html)
    links: list[DiscoveredLink] = []
    seen: set[str] = set()
    total_count = 0
    for anchor in collector.anchors:
        attrs = anchor["attrs"]
        href = unescape(str(attrs.get("href", "")).strip())
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        normalized_url = urljoin(source_url, href)
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        total_count += 1
        if len(links) >= max_links:
            continue
        text = re.sub(r"\s+", " ", unescape(" ".join(anchor["text"]))).strip()
        links.append(
            DiscoveredLink(
                url=href,
                normalized_url=normalized_url,
                text=text,
                rel=str(attrs.get("rel", "")).split(),
                source_url=source_url,
                host=parsed.hostname.lower(),
                is_same_host=parsed.hostname.lower() == source_host,
            )
        )
    return links, total_count, total_count > len(links)


def _format_extracted_document_text(text: str, *, source_url: str) -> str:
    """Normalize scraper-extracted document text for downstream annotation.

    This keeps agenda section numbers and item numbers attached to their related
    text before the text is handed to downstream tools such as Label Studio.
    """
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


def _default_document_text_extractor(body: bytes, content_type: str | None, url: str) -> str:
    normalized_content_type = (content_type or "").lower()
    if normalized_content_type.startswith("text/"):
        return _decode_body(body, content_type).strip()
    if "pdf" in normalized_content_type or url.split("?", 1)[0].lower().endswith(".pdf"):
        completed = subprocess.run(
            ["pdftotext", "-", "-"],
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.decode("utf-8", errors="replace").strip()
    return ""


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


def _default_trafilatura_extractor(html: str, url: str) -> str:
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        return _text_from_html(html)
    extracted = trafilatura.extract(html, url=url)
    return (extracted or "").strip()


# Firecrawl format extraction moved to app.crawl.firecrawl_format
from app.crawl.firecrawl_format import extract_firecrawl_format, _text_from_html



def _discovery_diagnostics(
    *,
    normalized_url: str,
    html: str | None,
    policy: dict[str, Any],
    headers: dict[str, str],
    http_fetcher: HttpFetcher,
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
            robots_headers = {key.lower(): value for key, value in robots_response.headers.items()}
            robots_text = _decode_body(robots_response.body, robots_headers.get("content-type"))
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
        except Exception as error:  # noqa: BLE001 - diagnostics should not fail crawl
            diagnostics["robots"] = {"url": robots_url, "error": str(error), "sitemaps": []}
    sitemap_urls: list[str] = []
    if discovery.get("sitemaps"):
        for sitemap_url in robots_sitemaps or [urljoin(origin, "/sitemap.xml")]:
            try:
                sitemap_response = http_fetcher(sitemap_url, headers, policy["limits"]["timeout_seconds"])
                sitemap_headers = {key.lower(): value for key, value in sitemap_response.headers.items()}
                sitemap_text = _decode_body(sitemap_response.body, sitemap_headers.get("content-type"))
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
        classification="binary_no_text",
        http_status=http_status,
        content_type=content_type,
        elapsed_ms=elapsed_ms,
        error="binary_no_text",
    )
    return UniversalCrawlResult(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status="failed",
        classification="binary_no_text",
        selected_strategy=None,
        attempted_strategies=[attempt],
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        html=None,
        text=None,
        extraction_method="binary_no_text",
        quality_score=0.0,
        error="binary_no_text",
    )


def _http_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    document_text_extractor: DocumentTextExtractor,
    trafilatura_extractor: TrafilaturaExtractor,
) -> UniversalCrawlResult:
    started = time.monotonic()
    retry_diagnostics: dict[str, Any] = {"attempts": 0, "transient_errors": []}
    request_headers = _headers_for_strategy(strategy, policy, normalized_url)
    try:
        response, retry_diagnostics = _fetch_with_retries(
            http_fetcher,
            normalized_url,
            request_headers,
            policy["limits"]["timeout_seconds"],
            policy["limits"]["max_retries"],
        )
        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type")
        if strategy != "DOCUMENT_EXTRACT" and _looks_like_binary_body(response.body, content_type, response.final_url):
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
        body_text = _decode_body(response.body, content_type)
        is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(("<html", "<!doctype html"))
        html = body_text if is_html else None
        if strategy == "DOCUMENT_EXTRACT":
            text = _format_extracted_document_text(document_text_extractor(response.body, content_type, response.final_url), source_url=response.final_url)
        elif strategy == "TRAFILATURA_EXTRACT" and is_html:
            text = trafilatura_extractor(body_text, response.final_url).strip()
        elif strategy == "TRAFILATURA_EXTRACT":
            text = ""
        else:
            text = _text_from_html(body_text) if is_html else body_text.strip()
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
        if strategy == "TRAFILATURA_EXTRACT" and not is_html and classification == "rendered":
            classification = "binary_no_text"
            classification_evidence = [{"classification": "binary_no_text", "signature": "non_html_body", "source": "content_type", "context": content_type or ""}]
        status = _status_for_classification(classification)
        extraction_method = "http_html" if is_html else "http_body"
        if strategy == "DOCUMENT_EXTRACT":
            extraction_method = "document_extract"
        elif strategy == "TRAFILATURA_EXTRACT":
            extraction_method = "trafilatura_extract"
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
            _discovery_diagnostics(
                normalized_url=normalized_url,
                html=html,
                policy=policy,
                headers=request_headers,
                http_fetcher=http_fetcher,
            )
        )
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
            extraction_method=extraction_method,
            quality_score=quality,
            classification_evidence=classification_evidence,
            diagnostics=diagnostics,
            discovered_link_count=discovered_link_count,
            discovered_links_truncated=discovered_links_truncated,
            discovered_links=discovered_links,
            error=None if status == "success" else classification,
        )
    except Exception as error:  # noqa: BLE001 - HTTP clients raise mixed network exceptions
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


async def _browser_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    policy: dict[str, Any],
    browser_worker: CrawlWorker,
) -> UniversalCrawlResult:
    options = RenderOptions(
        timeout_seconds=policy["limits"]["timeout_seconds"],
        max_retries=policy["limits"]["max_retries"],
        respect_robots=True,
        include_html=True,
        include_text=True,
        browser_profile=policy.get("browser_profile") or {},
    )
    job = CrawlJob(index=0, url=normalized_url, domain=registrable_domain(normalized_url), options=options)
    result = await _call_worker(browser_worker, job)
    extraction_method = result.metadata.get("extraction_method") or "dom_text"
    quality = _quality_score(result.status, result.classification, result.text, result.html, None)
    discovered_links, discovered_link_count, discovered_links_truncated = (
        _extract_discovered_links(result.html, result.final_url, max_links=policy["limits"]["max_discovered_links"]) if result.status == "success" else ([], 0, False)
    )
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status=result.status,
        classification=result.classification,
        http_status=None,
        content_type=None,
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )
    return UniversalCrawlResult(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=result.final_url,
        status=result.status,
        classification=result.classification,
        selected_strategy=strategy if result.status == "success" else None,  # type: ignore[arg-type]
        attempted_strategies=[attempt],
        http_status=None,
        headers={},
        content_type=None,
        html=result.html,
        text=result.text,
        extraction_method=extraction_method,
        quality_score=quality,
        classification_evidence=[],
        diagnostics={
            "browser_profile": policy.get("browser_profile") or {},
            "runtime": {"elapsed_ms": result.elapsed_ms, "final_url": result.final_url},
            "content_metrics": _content_metrics(result.text, result.html),
            **_html_diagnostics(result.html, result.final_url),
        },
        discovered_link_count=discovered_link_count,
        discovered_links_truncated=discovered_links_truncated,
        discovered_links=discovered_links,
        error=result.error,
    )


async def _execute_crawl_chain(
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    browser_worker: CrawlWorker | None,
    document_text_extractor: DocumentTextExtractor,
    trafilatura_extractor: TrafilaturaExtractor,
) -> UniversalCrawlResult:
    attempts: list[CrawlStrategyAttempt] = []
    last_result: UniversalCrawlResult | None = None
    for strategy in policy["strategies"]:
        if strategy in HTTP_STRATEGIES:
            # Use curl_cffi fetcher for HTTP_CFFI_IMPERSONATE strategy
            if strategy == "HTTP_CFFI_IMPERSONATE":
                strategy_fetcher = _default_cffi_fetcher
            else:
                strategy_fetcher = http_fetcher
            result = await asyncio.to_thread(
                _http_strategy_result,
                original_url=original_url,
                normalized_url=normalized_url,
                strategy=strategy,
                policy=policy,
                http_fetcher=strategy_fetcher,
                document_text_extractor=document_text_extractor,
                trafilatura_extractor=trafilatura_extractor,
            )
        elif strategy in BROWSER_STRATEGIES:
            if browser_worker is None:
                result = UniversalCrawlResult(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    final_url=None,
                    status="failed",
                    classification="browser_crash",
                    selected_strategy=None,
                    attempted_strategies=[
                        CrawlStrategyAttempt(
                            strategy=strategy,  # type: ignore[arg-type]
                            status="failed",
                            classification="browser_crash",
                            http_status=None,
                            content_type=None,
                            elapsed_ms=0,
                            error="browser_worker_unavailable",
                        )
                    ],
                    http_status=None,
                    headers={},
                    content_type=None,
                    html=None,
                    text=None,
                    extraction_method=None,
                    quality_score=0.0,
                    diagnostics={"browser_profile": policy.get("browser_profile") or {}, "runtime": {"error": "browser_worker_unavailable"}},
                    error="browser_worker_unavailable",
                )
            else:
                result = await _browser_strategy_result(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    strategy=strategy,
                    policy=policy,
                    browser_worker=browser_worker,
                )
        else:
            result = UniversalCrawlResult(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=None,
                status="failed",
                classification="unknown_error",
                selected_strategy=None,
                attempted_strategies=[
                    CrawlStrategyAttempt(
                        strategy=strategy,  # type: ignore[arg-type]
                        status="failed",
                        classification="unknown_error",
                        http_status=None,
                        content_type=None,
                        elapsed_ms=0,
                        error="unsupported_strategy",
                    )
                ],
                http_status=None,
                headers={},
                content_type=None,
                html=None,
                text=None,
                extraction_method=None,
                quality_score=0.0,
                error="unsupported_strategy",
            )
        attempts.extend(result.attempted_strategies)
        last_result = result
        if result.status == "success" and result.quality_score > 0:
            return UniversalCrawlResult(**{**asdict(result), "attempted_strategies": attempts})
    assert last_result is not None
    return UniversalCrawlResult(**{**asdict(last_result), "attempted_strategies": attempts})


async def crawl_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    browser_worker: CrawlWorker | None = None,
    http_fetcher: HttpFetcher | None = None,
    document_text_extractor: DocumentTextExtractor | None = None,
    trafilatura_extractor: TrafilaturaExtractor | None = None,
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")
    policy = _payload_policy(payload, active_settings)
    fetcher = http_fetcher or _default_http_fetcher
    active_document_text_extractor = document_text_extractor or _default_document_text_extractor
    active_trafilatura_extractor = trafilatura_extractor or _default_trafilatura_extractor

    immediate_results: dict[int, UniversalCrawlResult] = {}
    jobs: list[CrawlExecutionJob] = []
    for index, original_url in enumerate(urls):
        try:
            normalized_url = normalize_public_http_url(original_url)
            domain = registrable_domain(normalized_url)
        except ValueError as error:
            # Raise validation error to be caught by FastAPI exception handler
            raise ValueError(f"invalid_url: {error}") from error
        jobs.append(CrawlExecutionJob(index=index, original_url=original_url, normalized_url=normalized_url, domain=domain))

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )

    async def run_job(job: CrawlExecutionJob) -> tuple[int, UniversalCrawlResult]:
        index = job.index
        original_url = job.original_url
        normalized_url = job.normalized_url
        result = await _execute_crawl_chain(
            original_url=original_url,
            normalized_url=normalized_url,
            policy=policy,
            http_fetcher=fetcher,
            browser_worker=browser_worker,
            document_text_extractor=active_document_text_extractor,
            trafilatura_extractor=active_trafilatura_extractor,
        )
        return index, result

    scheduled_pairs = await scheduler.run(jobs, run_job) if jobs else []
    by_index = dict(immediate_results)
    for index, result in scheduled_pairs:
        by_index[index] = result

    ordered_results = [by_index[index] for index in range(len(urls))]
    return {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        "policy_schema_version": policy["schema_version"],
        "results": [_universal_result_dict(result) for result in ordered_results],
    }


async def render_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    worker: CrawlWorker,
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")

    options = _render_options(payload, active_settings)
    started = time.monotonic()
    immediate_results: dict[int, CrawlResult] = {}
    jobs: list[CrawlJob] = []

    for index, url in enumerate(urls):
        try:
            normalized = normalize_public_http_url(url)
            domain = registrable_domain(normalized)
        except ValueError as error:
            immediate_results[index] = _disallowed_result((index, url), str(error), int((time.monotonic() - started) * 1000))
            continue
        jobs.append(CrawlJob(index=index, url=normalized, domain=domain, options=options))

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )
    scheduled_results = await scheduler.run(jobs, lambda job: _call_worker(worker, job)) if jobs else []

    by_index = dict(immediate_results)
    for result in scheduled_results:
        matching_job = next(job for job in jobs if job.url == result.url)
        by_index[matching_job.index] = result

    ordered_results = [by_index[index] for index in range(len(urls))]
    return {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        "results": [_result_dict(result) for result in ordered_results],
    }
