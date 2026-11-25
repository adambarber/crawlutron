"""Owns retry policy, binary detection, and discovery diagnostics.

reads datetime for expiry calculation

wall-consumer
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import re
from collections.abc import Iterable, Mapping
from typing import Final, cast

from crawlutron.admission.policy import DiscoveryFlags, discovery_flags
from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.discovery_diagnostics import DiscoveryDiagnostics
from crawlutron.models.fetch_envelope import RetryDiagnostics
from crawlutron.models.http_fetch_result import HttpFetchResult as _HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.models.robots_diagnostics import RobotsDiagnostics
from crawlutron.transport.body_decode import decode_body
from crawlutron.transport.classification import classify_error_code
from crawlutron.transport.decompression import decompress_body as _decompress_body
from crawlutron.transport.robots_parse import parse_robots_crawl_delay, parse_robots_sitemaps
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES, DISCOVERY_BODY_MAX_BYTES
from crawlutron.vocabulary.taxonomy import is_transient_classification
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

#: Maximum ``Sitemap:`` directives honoured per robots.txt. Beyond it excess
#: lines are silently dropped so a hostile file cannot spawn a fan-out.
_ROBOTS_SITEMAP_DIRECTIVE_CAP: Final[int] = 20

_RETRY_BACKOFF_BASE_SECONDS = 0.25
_RETRY_BACKOFF_CAP_SECONDS = 2.0


def retry_backoff_seconds(attempt: int) -> float:
    """Capped exponential gap before the retry after a failed ``attempt``: doubles
    from the base and plateaus at the ceiling, so a failing origin is not hammered
    back-to-back yet a large retry budget cannot park a URL for minutes.
    """
    return min(_RETRY_BACKOFF_CAP_SECONDS, _RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


# An HTTP-date Retry-After is a politeness cooldown like the integer form, so it
# rides the same hostile-value clamp (an unbounded date parks coroutines for years).
_RETRY_AFTER_MAX_SECONDS = 300


def retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    """The Retry-After cooldown, from the delay-seconds or the HTTP-date form.

    The date form converts against the wall clock (an HTTP-date IS a wall instant);
    a past date clamps to 0, and both forms clamp to the hostile-value ceiling.
    """
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        # Clamp hostile values: an unbounded Retry-After would park scheduler
        # coroutines in cooldown sleep for years.
        return min(_RETRY_AFTER_MAX_SECONDS, max(0, int(value.strip())))
    except ValueError:
        pass
    instant = _parsed_http_date(value.strip())
    if instant is None:
        return None
    remaining = (instant - datetime.datetime.now(datetime.UTC)).total_seconds()
    return min(_RETRY_AFTER_MAX_SECONDS, max(0, int(remaining)))


def _parsed_http_date(value: str) -> datetime.datetime | None:
    """The RFC 7231 IMF-fixdate (the only form Retry-After carries), or None."""
    try:
        return datetime.datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=datetime.UTC)
    except ValueError:
        return None


async def fetch_with_retries(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: dict[str, str],
    timeout_seconds: float,
    max_retries: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
) -> tuple[_HttpFetchResult, RetryDiagnostics]:
    transient_errors: list[str] = []
    max_attempts = max(1, max_retries + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            response = fetcher(url, headers, timeout_seconds, max_content_length)
            if inspect.iscoroutine(response):
                response = await response
            # The fetcher Protocol returns HttpFetchResult; the local may have held
            # the sync-call coroutine until the await above narrowed it.
            return cast(_HttpFetchResult, response), {
                "attempts": attempt,
                "transient_errors": transient_errors,
                "content_cache": "miss",
            }
        except Exception as error:
            classified = classify_error_code(error)
            transient_errors.append(classified)
            # The taxonomy's OWN law decides whether another pass is worth it;
            # a terminal refusal (SSRF pin, value_error, ...) re-raises at once.
            if attempt >= max_attempts or not is_transient_classification(classified):
                raise
            # Capped exponential backoff so a failing origin sees widening
            # gaps; the bound keeps a large retry budget from parking a URL.
            await asyncio.sleep(retry_backoff_seconds(attempt))
    # ``max_attempts`` is floored at 1, so every call returns or re-raises;
    # stated because mypy cannot derive the loop's totality from the floor.
    raise AssertionError("unreachable: retry loop is total")


def looks_like_binary_body(body: bytes, content_type: str | None, url: NormalizedUrl) -> bool:
    url_text = url.raw
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("text/") or normalized_content_type in {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
    }:
        return False
    if "html" in normalized_content_type or "pdf" in normalized_content_type:
        return False
    lower_url = url_text.split("?", 1)[0].lower()
    if lower_url.endswith((".zip", ".gz", ".tgz", ".7z", ".rar", ".xlsx", ".docx")):
        return True
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b")):
        return True
    return normalized_content_type in {
        "application/octet-stream",
        "application/zip",
        "application/x-zip-compressed",
    }


#: Every key a ``diagnostics["robots"]`` block carries, with the answer
#: for a robots.txt never consulted: the branches must agree on the shape.
_ROBOTS_DIAGNOSTICS: RobotsDiagnostics = {
    "url": None,
    "allowed": True,
    "crawl_delay_seconds": None,
    "sitemaps": [],
    "reused_from_engine": False,
    "evaluation": "disabled",
}


def _robots_diagnostics(evaluation: str, **overrides: object) -> RobotsDiagnostics:
    """The ``diagnostics["robots"]`` block for one outcome: the base supplies every
    key, ``evaluation`` names how the answer was reached, and ``allowed`` stays
    True on the failure path -- a False would read as a denial.
    """
    base: RobotsDiagnostics = {**_ROBOTS_DIAGNOSTICS, "evaluation": evaluation}
    return cast(RobotsDiagnostics, {**base, **overrides})


async def _fetch_robots_text(
    http_fetcher: HttpFetcher, robots_url: NormalizedUrl, headers: dict[str, str], timeout_seconds: float
) -> str:
    """Fetch + decode one robots.txt body, tolerating a sync test double.

    The fetch is bounded by ``timeout_seconds`` via ``asyncio.wait_for``.
    """
    robots_response = http_fetcher(robots_url, headers, timeout_seconds)
    if inspect.iscoroutine(robots_response):
        robots_response = await asyncio.wait_for(robots_response, timeout=timeout_seconds)
    response: _HttpFetchResult = cast(_HttpFetchResult, robots_response)
    robots_headers = {key.lower(): value for key, value in response.headers.items()}
    return decode_body(
        _decompress_body(
            response.body,
            robots_headers.get("content-encoding", ""),
            max_output_bytes=DISCOVERY_BODY_MAX_BYTES,
        ),
        robots_headers.get("content-type"),
    )


async def _fetch_robot_directives(
    *,
    normalized_url: NormalizedUrl,
    http_fetcher: HttpFetcher,
    headers: dict[str, str],
    timeout_seconds: float,
) -> tuple[float | None, list[str], int]:
    """Fetch /robots.txt and fold its crawl-delay and Sitemap directives.

    Returns ``(delay, sitemaps, dropped)`` with the discarded directive count."""
    robots_url = normalized_url.resolve("/robots.txt")
    if robots_url is None:
        raise ValueError("no in-scope /robots.txt")
    robots_text = await _fetch_robots_text(http_fetcher, robots_url, headers, timeout_seconds)
    crawl_delay = parse_robots_crawl_delay(robots_text)
    resolved_sitemaps, _total, dropped = _resolve_sitemap_urls(
        parse_robots_sitemaps(robots_text), normalized_url
    )
    return crawl_delay, resolved_sitemaps, dropped


def _resolve_sitemap_urls(
    candidates: Iterable[str], normalized_url: NormalizedUrl
) -> tuple[list[str], int, int]:
    """Resolve Sitemap directives to at most the cap of SAME-SITE urls.

    ``resolve`` admits foreign hosts, so the site filter is the security
    boundary; the cap bounds accepted urls, not raw directive lines."""
    resolved: list[str] = []
    target_site = normalized_url.site
    total_seen = 0
    for entry in candidates:
        total_seen += 1
        if len(resolved) >= _ROBOTS_SITEMAP_DIRECTIVE_CAP:
            continue
        admitted = normalized_url.resolve(entry)
        if admitted is not None and admitted.site == target_site:
            resolved.append(admitted.raw)
    return resolved, total_seen, total_seen - len(resolved)


async def _diagnose_robots(
    *,
    normalized_url: NormalizedUrl,
    headers: dict[str, str],
    http_fetcher: HttpFetcher,
    timeout_seconds: float,
    flags: DiscoveryFlags,
    engine_robots_sitemaps: list[str] | None,
    robots_evaluated: bool,
) -> tuple[RobotsDiagnostics | None, list[str], int]:
    """Resolve the ``robots`` block and sitemap list for diagnostics.

    Returns ``(diag, sitemaps, drop_count)``; ``diag`` is None when skipped."""
    robots_sitemaps: list[str] = []
    directive_drop_count: int = 0
    if not robots_evaluated:
        return _robots_diagnostics("disabled"), robots_sitemaps, directive_drop_count
    if engine_robots_sitemaps is not None:
        robots_sitemaps, _total, dropped = _resolve_sitemap_urls(engine_robots_sitemaps, normalized_url)
        directive_drop_count = dropped
        return (
            _robots_diagnostics("cached", sitemaps=robots_sitemaps, reused_from_engine=True),
            robots_sitemaps,
            directive_drop_count,
        )
    if not flags.robots:
        return None, robots_sitemaps, directive_drop_count
    try:
        crawl_delay, robots_sitemaps, dropped = await _fetch_robot_directives(
            normalized_url=normalized_url,
            http_fetcher=http_fetcher,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        directive_drop_count = dropped
        robots_url = normalized_url.resolve("/robots.txt")
        return (
            _robots_diagnostics(
                "evaluated",
                url=robots_url.raw if robots_url is not None else None,
                crawl_delay_seconds=crawl_delay,
                sitemaps=robots_sitemaps,
            ),
            robots_sitemaps,
            directive_drop_count,
        )
    except Exception as error:
        robots_url = normalized_url.resolve("/robots.txt")
        return (
            _robots_diagnostics(
                "evaluated",
                url=robots_url.raw if robots_url is not None else None,
                error=classify_error_code(error),
            ),
            robots_sitemaps,
            directive_drop_count,
        )


async def _collect_sitemaps(
    *,
    normalized_url: NormalizedUrl,
    http_fetcher: HttpFetcher,
    host_policy: HostPolicy,
    timeout_seconds: float,
    candidate_urls: list[str],
) -> tuple[list[str], dict[str, str], int]:
    """Fetch one or more sitemaps down to ``(loc, lastmod)`` pairs.

    Returns ``(locs, lastmod, fetch_count)`` with the document fetch count.
    """
    locs: list[str] = []
    lastmod: dict[str, str] = {}
    fetch_count: int = 0
    from crawlutron.transport.fetch_helpers import fetch_sitemap_urls

    for sitemap_url in (a for a in (normalized_url.resolve(e) for e in candidate_urls) if a is not None):
        fetch_count += 1
        with contextlib.suppress(Exception):
            found_locs = await fetch_sitemap_urls(
                http_fetcher, sitemap_url, timeout_seconds, host_policy=host_policy
            )
            for loc, timestamp in found_locs:
                locs.append(loc.raw)
                if timestamp:
                    lastmod[loc.raw] = timestamp
    return locs, lastmod, fetch_count


def _collect_feeds(html: str | None, normalized_url: NormalizedUrl) -> list[str]:
    """The alternate rel=feed URLs named in ``html`` that resolve in scope."""
    feeds: list[str] = []
    for match in re.finditer(
        r"<link\b[^>]*rel=['\"][^'\"]*alternate[^'\"]*['\"][^>]*>", html or "", flags=re.IGNORECASE
    ):
        tag = match.group(0)
        if "rss" not in tag.lower() and "atom" not in tag.lower():
            continue
        href_match = re.search(r"href=['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
        if href_match:
            feed = normalized_url.resolve(href_match.group(1))
            if feed is not None:
                feeds.append(feed.raw)
    return feeds


async def discovery_diagnostics(
    *,
    normalized_url: NormalizedUrl,
    html: str | None,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
    headers: dict[str, str],
    http_fetcher: HttpFetcher,
    engine_robots_sitemaps: list[str] | None,
    robots_evaluated: bool,
) -> CrawlDiagnostics:
    flags = discovery_flags(policy)
    limits = policy.limits
    diagnostics: CrawlDiagnostics = {}
    directive_drop_count: int = 0
    robots_diag, robots_sitemaps, dropped = await _diagnose_robots(
        normalized_url=normalized_url,
        flags=flags,
        timeout_seconds=limits.timeout_seconds,
        headers=headers,
        http_fetcher=http_fetcher,
        engine_robots_sitemaps=engine_robots_sitemaps,
        robots_evaluated=robots_evaluated,
    )
    directive_drop_count = dropped
    if robots_diag is not None:
        diagnostics["robots"] = robots_diag
    sitemap_urls: list[str] = []
    fetch_count: int = 0
    if flags.sitemaps:
        candidate_sitemaps = robots_sitemaps or ["/sitemap.xml"]
        sitemap_urls, sitemap_lastmod, fetch_count = await _collect_sitemaps(
            normalized_url=normalized_url,
            http_fetcher=http_fetcher,
            host_policy=host_policy,
            timeout_seconds=limits.timeout_seconds,
            candidate_urls=candidate_sitemaps,
        )
        if sitemap_lastmod:
            diagnostics["sitemap_lastmod"] = sitemap_lastmod
    feeds: list[str] = []
    if flags.feeds:
        feeds = _collect_feeds(html, normalized_url)
    if sitemap_urls or feeds:
        discovery: DiscoveryDiagnostics = {"sitemap_urls": sitemap_urls, "feeds": feeds}
        if directive_drop_count:
            discovery["directive_drop_count"] = directive_drop_count
        if fetch_count:
            discovery["fetch_count"] = fetch_count
        diagnostics["discovery"] = discovery
    return diagnostics


__all__ = [
    "discovery_diagnostics",
    "fetch_with_retries",
    "looks_like_binary_body",
    "retry_after_seconds",
    "retry_backoff_seconds",
]
