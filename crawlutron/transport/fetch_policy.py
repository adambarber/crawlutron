"""Owns retry policy, binary detection, and discovery diagnostics.

This module owns bounded retry/backoff behavior and post-fetch discovery probes.
It must not own client construction, body decoding, extraction, or result assembly.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import re
from collections.abc import Iterable
from typing import cast

from crawlutron.admission.policy import DiscoveryFlags, discovery_flags
from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.fetch_envelope import RetryDiagnostics
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.http_fetch_result import HttpFetchResult as _HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.robots_diagnostics import RobotsDiagnostics
from crawlutron.transport.body_decode import decode_body
from crawlutron.transport.classification import classify_error_code
from crawlutron.transport.decompression import decompress_body as _decompress_body
from crawlutron.transport.robots_parse import parse_robots_crawl_delay, parse_robots_sitemaps
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES, DISCOVERY_BODY_MAX_BYTES

_RETRY_BACKOFF_BASE_SECONDS = 0.25
_RETRY_BACKOFF_CAP_SECONDS = 2.0


def retry_backoff_seconds(attempt: int) -> float:
    """Capped exponential gap before the retry after a failed ``attempt``: doubles
    from the base and plateaus at the ceiling, so a failing origin is not hammered
    back-to-back yet a large retry budget cannot park a URL for minutes.
    """
    return min(_RETRY_BACKOFF_CAP_SECONDS, _RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


# An HTTP-date Retry-After is a politeness cooldown like the integer form, so
# it rides the same hostile-value clamp: an unbounded date would park scheduler
# coroutines in cooldown sleep for years.
_RETRY_AFTER_MAX_SECONDS = 300


def retry_after_seconds(headers: dict[str, str]) -> int | None:
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
    timeout_seconds: int,
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
            transient_errors.append(classify_error_code(error))
            if attempt >= max_attempts:
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
    http_fetcher: HttpFetcher, robots_url: NormalizedUrl, headers: dict[str, str], timeout_seconds: int
) -> str:
    """Fetch + decode one robots.txt body, tolerating a sync test double."""
    robots_response = http_fetcher(robots_url, headers, timeout_seconds)
    if inspect.iscoroutine(robots_response):
        # The protocol's return is a coroutine when the fetcher is async.
        robots_response = await robots_response
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
    timeout_seconds: int,
) -> tuple[float | None, list[str]]:
    """Fetch /robots.txt and fold its crawl-delay and Sitemap directives.

    Returns ``(crawl_delay, sitemaps)``; a failed fetch raises so the caller can
    classify it into a single error ``robots`` block."""
    robots_url = normalized_url.resolve("/robots.txt")
    if robots_url is None:
        raise ValueError("no in-scope /robots.txt")
    robots_text = await _fetch_robots_text(http_fetcher, robots_url, headers, timeout_seconds)
    crawl_delay = parse_robots_crawl_delay(robots_text)
    sitemaps = _resolve_sitemap_urls(parse_robots_sitemaps(robots_text), normalized_url)
    return crawl_delay, sitemaps


def _resolve_sitemap_urls(candidates: Iterable[str], normalized_url: NormalizedUrl) -> list[str]:
    """The candidates that resolve in this request's scope, as raw URLs."""
    return [admitted.raw for entry in candidates if (admitted := normalized_url.resolve(entry)) is not None]


async def _diagnose_robots(
    *,
    normalized_url: NormalizedUrl,
    headers: dict[str, str],
    http_fetcher: HttpFetcher,
    timeout_seconds: int,
    flags: DiscoveryFlags,
    engine_robots_sitemaps: list[str] | None,
    robots_evaluated: bool,
) -> tuple[RobotsDiagnostics | None, list[str]]:
    """Resolve the ``robots`` block and reusable sitemap list for diagnostics.

    A ``None`` first value means the caller should not set the ``robots`` key."""
    robots_sitemaps: list[str] = []
    if not robots_evaluated:
        return _robots_diagnostics("disabled"), robots_sitemaps
    if engine_robots_sitemaps is not None:
        robots_sitemaps = _resolve_sitemap_urls(engine_robots_sitemaps, normalized_url)
        return _robots_diagnostics(
            "cached", sitemaps=robots_sitemaps, reused_from_engine=True
        ), robots_sitemaps
    if not flags.robots:
        return None, robots_sitemaps
    try:
        crawl_delay, robots_sitemaps = await _fetch_robot_directives(
            normalized_url=normalized_url,
            http_fetcher=http_fetcher,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        robots_url = normalized_url.resolve("/robots.txt")
        return _robots_diagnostics(
            "evaluated",
            url=robots_url.raw if robots_url is not None else None,
            crawl_delay_seconds=crawl_delay,
            sitemaps=robots_sitemaps,
        ), robots_sitemaps
    except Exception as error:
        robots_url = normalized_url.resolve("/robots.txt")
        return _robots_diagnostics(
            "evaluated",
            url=robots_url.raw if robots_url is not None else None,
            error=classify_error_code(error),
        ), robots_sitemaps


async def _collect_sitemaps(
    *,
    normalized_url: NormalizedUrl,
    http_fetcher: HttpFetcher,
    host_policy: HostPolicy,
    timeout_seconds: int,
    candidate_urls: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Fetch one or more sitemaps down to ``(loc, lastmod)`` pairs."""
    locs: list[str] = []
    lastmod: dict[str, str] = {}
    from crawlutron.transport.fetch_helpers import fetch_sitemap_urls

    for sitemap_url in (a for a in (normalized_url.resolve(e) for e in candidate_urls) if a is not None):
        with contextlib.suppress(Exception):
            found_locs = await fetch_sitemap_urls(
                http_fetcher, sitemap_url, timeout_seconds, host_policy=host_policy
            )
            for loc, timestamp in found_locs:
                locs.append(loc.raw)
                if timestamp:
                    lastmod[loc.raw] = timestamp
    return locs, lastmod


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
    robots_diag, robots_sitemaps = await _diagnose_robots(
        normalized_url=normalized_url,
        flags=flags,
        timeout_seconds=limits.timeout_seconds,
        headers=headers,
        http_fetcher=http_fetcher,
        engine_robots_sitemaps=engine_robots_sitemaps,
        robots_evaluated=robots_evaluated,
    )
    if robots_diag is not None:
        diagnostics["robots"] = robots_diag
    sitemap_urls: list[str] = []
    if flags.sitemaps:
        candidate_sitemaps = robots_sitemaps or ["/sitemap.xml"]
        sitemap_urls, sitemap_lastmod = await _collect_sitemaps(
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
        diagnostics["discovery"] = {"sitemap_urls": sitemap_urls, "feeds": feeds}
    return diagnostics


__all__ = [
    "discovery_diagnostics",
    "fetch_with_retries",
    "looks_like_binary_body",
    "retry_after_seconds",
    "retry_backoff_seconds",
]
