"""Owns retry policy, binary detection, and discovery diagnostics.

This module owns bounded retry/backoff behavior and post-fetch discovery probes.
It must not own client construction, body decoding, extraction, or result assembly.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
from typing import Any

from app.admission.policy import discovery_flags
from app.models.crawl_policy import CrawlPolicy
from app.models.host_policy import HostPolicy
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.orchestration.result_helpers import classify_error_code
from app.transport.body_decode import decode_body
from app.transport.decompression import decompress_body as _decompress_body
from app.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES, DISCOVERY_BODY_MAX_BYTES

_RETRY_BACKOFF_BASE_SECONDS = 0.25
_RETRY_BACKOFF_CAP_SECONDS = 2.0


def retry_backoff_seconds(attempt: int) -> float:
    """Capped exponential gap before the retry following a failed ``attempt``.

    Doubles from the base and plateaus at the ceiling: one failing origin
    must not be hammered back-to-back, yet the wait stays bounded so a large
    ``max_retries`` budget cannot park a URL for minutes.
    """
    return min(_RETRY_BACKOFF_CAP_SECONDS, _RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def retry_after_seconds(headers: dict[str, str]) -> int | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        # Clamp hostile values: an unbounded Retry-After would park scheduler
        # coroutines in cooldown sleep for years.
        return min(300, max(0, int(value.strip())))
    except ValueError:
        return None


async def fetch_with_retries(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: dict[str, str],
    timeout_seconds: int,
    max_retries: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
) -> tuple[Any, dict[str, Any]]:
    transient_errors: list[str] = []
    max_attempts = max(1, max_retries + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            response = fetcher(url, headers, timeout_seconds, max_content_length)
            if inspect.iscoroutine(response):
                response = await response
            return response, {"attempts": attempt, "transient_errors": transient_errors}
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
_ROBOTS_DIAGNOSTICS: dict[str, Any] = {
    "url": None,
    "allowed": True,
    "crawl_delay_seconds": None,
    "sitemaps": [],
    "reused_from_engine": False,
}


def _robots_diagnostics(evaluation: str, **overrides: Any) -> dict[str, Any]:
    """The ``diagnostics["robots"]`` block for one robots outcome.

    The base above supplies every key; ``evaluation`` says how the answer was
    reached -- ``disabled`` (robots not consulted), ``cached`` (the engine already
    fetched it), or ``evaluated`` (fetched here, possibly unsuccessfully) -- and
    ``overrides`` carries what this branch knows.

    Every URL in the result was resolved through ``NormalizedUrl.resolve`` with the
    request's host policy *before* it got here; this function resolves nothing,
    so it is a packer and not a URL egress door, and it deliberately does not take
    a ``HostPolicy`` to look like one. The alternative -- a signature naming a
    policy it would never consult -- is a lie the reader has to disprove.

    ``allowed`` stays True on the failure path on purpose: the caller falls back
    to its own policy when robots could not be read, and a False here would read
    as a denial that never happened.
    """
    return {**_ROBOTS_DIAGNOSTICS, "sitemaps": [], "evaluation": evaluation, **overrides}


async def discovery_diagnostics(
    *,
    normalized_url: NormalizedUrl,
    html: str | None,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
    headers: dict[str, str],
    http_fetcher: Any,
    engine_robots_sitemaps: list[str] | None,
    robots_evaluated: bool,
) -> dict[str, Any]:
    flags = discovery_flags(policy)
    limits = policy.limits
    diagnostics: dict[str, Any] = {}
    safe_source = normalized_url
    robots_sitemaps: list[str] = []
    if not robots_evaluated:
        diagnostics["robots"] = _robots_diagnostics("disabled")
    elif engine_robots_sitemaps is not None:
        # Reuse the /robots.txt the engine already fetched (allow/deny and
        # Sitemap directives); an empty list is a valid "no sitemaps" signal.
        robots_sitemaps = [
            admitted.raw
            for entry in engine_robots_sitemaps
            if (admitted := safe_source.resolve(entry)) is not None
        ]
        diagnostics["robots"] = _robots_diagnostics(
            "cached", sitemaps=robots_sitemaps, reused_from_engine=True
        )
    elif flags.robots:
        robots_url = safe_source.resolve("/robots.txt")
        if robots_url is None:
            return diagnostics
        try:
            # The fetch_helpers chokepoint's sync-double tolerance: the honest
            # type is the coroutine; a sync double returns the result directly.
            robots_response: Any = http_fetcher(robots_url, headers, limits.timeout_seconds)
            if inspect.iscoroutine(robots_response):
                robots_response = await robots_response
            robots_headers = {key.lower(): value for key, value in robots_response.headers.items()}
            robots_text = decode_body(
                _decompress_body(
                    robots_response.body,
                    robots_headers.get("content-encoding", ""),
                    max_output_bytes=DISCOVERY_BODY_MAX_BYTES,
                ),
                robots_headers.get("content-type"),
            )
            crawl_delay = None
            for line in robots_text.splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "crawl-delay":
                    try:
                        crawl_delay = int(value.strip())
                    except ValueError:
                        crawl_delay = None
                elif key.strip().lower() == "sitemap":
                    safe_sitemap = safe_source.resolve(value.strip())
                    if safe_sitemap is not None:
                        robots_sitemaps.append(safe_sitemap.raw)
            diagnostics["robots"] = _robots_diagnostics(
                "evaluated",
                url=robots_url.raw,
                crawl_delay_seconds=crawl_delay,
                sitemaps=robots_sitemaps,
            )
        except Exception as error:
            diagnostics["robots"] = _robots_diagnostics(
                "evaluated", url=robots_url.raw, error=classify_error_code(error)
            )
    sitemap_urls: list[str] = []
    if flags.sitemaps:
        candidate_sitemaps = robots_sitemaps or ["/sitemap.xml"]
        from app.orchestration.fetch_helpers import fetch_sitemap_urls

        admitted_sitemaps = (safe_source.resolve(entry) for entry in candidate_sitemaps)
        for sitemap_url in (a for a in admitted_sitemaps if a is not None):
            with contextlib.suppress(Exception):
                # Recursively resolves sitemap indexes down to (loc, lastmod) pairs.
                found_locs = await fetch_sitemap_urls(
                    http_fetcher,
                    sitemap_url,
                    limits.timeout_seconds,
                    host_policy=host_policy,
                )
                for loc, lastmod in found_locs:
                    sitemap_urls.append(loc.raw)
                    if lastmod:
                        diagnostics.setdefault("sitemap_lastmod", {})[loc.raw] = lastmod
    feeds: list[str] = []
    if flags.feeds:
        for match in re.finditer(
            r"<link\b[^>]*rel=['\"][^'\"]*alternate[^'\"]*['\"][^>]*>", html or "", flags=re.IGNORECASE
        ):
            tag = match.group(0)
            if "rss" not in tag.lower() and "atom" not in tag.lower():
                continue
            href_match = re.search(r"href=['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
            if href_match:
                feed = safe_source.resolve(href_match.group(1))
                if feed is not None:
                    feeds.append(feed.raw)
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
