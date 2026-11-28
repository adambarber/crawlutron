"""Namespaced browser cookie store with harvest helpers."""

from __future__ import annotations

import json
import logging
from typing import cast

from crawlutron.runtime import process_state
from crawlutron.runtime.resource_lifecycle import record_declined_close
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE, CookieRecord, sendable_cookie_header
from crawlutron.transport.cookie_store import InMemoryCookieStore
from crawlutron.transport.render_page import RenderPage
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl
from safe_url.url_redaction import redact_urls_in_text

logger = logging.getLogger(__name__)


__all__ = [
    "BROWSER_COOKIES_BY_DOMAIN",
    "BROWSER_COOKIES_MAX",
    "harvest_session_cookies",
    "store_browser_cookies",
]


def _reset() -> None:
    """Drop every cookie the renderer has accumulated, in every namespace."""
    BROWSER_COOKIES_BY_DOMAIN.clear()


def _has() -> bool:
    """Whether any namespace holds a cookie right now."""
    return len(BROWSER_COOKIES_BY_DOMAIN) > 0


process_state.register(__name__, _reset, _has)


BROWSER_COOKIES_MAX = 500


def _split_header(header: str) -> dict[str, str]:
    """The name->value mapping a coalesced HTTP ``Cookie`` header holds."""
    parsed: dict[str, str] = {}
    for part in header.split(";"):
        name, _, value = part.partition("=")
        if name.strip():
            parsed[name.strip()] = value.strip()
    return parsed


def _bare_records(header: str) -> dict[str, CookieRecord]:
    """Host-only session records for a bare ``name=value`` header."""
    return {
        name: CookieRecord(value=value, domain="", path="/", secure=False, expires=None, host_only=True)
        for name, value in _split_header(header).items()
    }


def _running_namespace(namespace: str | None) -> str:
    """The namespace a cookie op keys by: an explicit one, else the crawl's.

    ``None`` defers to ``COOKIE_NAMESPACE`` so the harvest and the retry land
    in the same caller's slot without a parameter chain through the worker.
    """
    if namespace is not None:
        return namespace
    return COOKIE_NAMESPACE.get()


class _LockedDomainCookieStore:
    """Browser-harvest store: RFC 6265 records keyed by exact host, per namespace."""

    __slots__ = ("_store",)

    def __init__(self, max_entries: int) -> None:
        self._store = InMemoryCookieStore(max_hosts_per_namespace=max_entries)

    def store(self, host: str, records: dict[str, CookieRecord], namespace: str = "") -> None:
        """Pin each record to the exact harvest host as host-only, then store."""
        payloads = {
            name: json.dumps({**record, "domain": host, "host_only": True})
            for name, record in records.items()
        }
        self._store.put(namespace, host, payloads)

    def touch_get(
        self, host: str, namespace: str = "", *, scheme: str = "https", path: str = "/"
    ) -> str | None:
        """The header RFC 6265 allows for this host, decoded from the caller's slot."""
        payloads = self._store.load(namespace, host)
        if not payloads:
            return None
        records = [
            (name, host, cast("CookieRecord", json.loads(payload))) for name, payload in payloads.items()
        ]
        url = NormalizedUrl(f"{scheme}://{host}{path or '/'}", policy=HostPolicy.unrestricted())
        header, _ = sendable_cookie_header(records, url)
        return header or None

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


BROWSER_COOKIES_BY_DOMAIN = _LockedDomainCookieStore(BROWSER_COOKIES_MAX)


def store_browser_cookies(
    domain: str,
    header: str = "",
    *,
    namespace: str | None = None,
    records: dict[str, CookieRecord] | None = None,
) -> None:
    """Record harvested cookies pinned to the exact host, in the caller's namespace."""
    BROWSER_COOKIES_BY_DOMAIN.store(
        domain,
        _bare_records(header) if records is None else records,
        _running_namespace(namespace),
    )


def get_browser_cookies_for(
    domain: str, *, namespace: str | None = None, scheme: str = "https", path: str = "/"
) -> str | None:
    """The RFC 6265 header for ``domain`` from the caller's harvested session."""
    return BROWSER_COOKIES_BY_DOMAIN.touch_get(
        domain, _running_namespace(namespace), scheme=scheme, path=path
    )


async def harvest_session_cookies(
    page: RenderPage, url: NormalizedUrl, *, namespace: str | None = None
) -> None:
    """Capture the page's cookies before the context closes.

    Runs synchronously: the retry reads the store right after the render."""
    try:
        context = page.context
        cookies = await context.cookies(url.raw)
        records: dict[str, CookieRecord] = {}
        for cookie in cookies:
            name = cookie.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_expires = cookie.get("expires")
            expiry = float(raw_expires) if isinstance(raw_expires, (int, float)) else None
            records[name] = CookieRecord(
                value=str(cookie.get("value", "")),
                domain=url.host,
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure", False)),
                expires=expiry if expiry is not None and expiry >= 0 else None,
                host_only=True,
            )
        if records:
            store_browser_cookies(url.host, namespace=namespace, records=records)
            logger.info("browser_cookies_harvested", extra={"domain": url.host, "count": len(cookies)})
    except Exception as error:
        record_declined_close("harvest_failed", type(error).__name__)
        logger.warning(
            "browser_cookies_harvest_failed",
            extra={"error": redact_urls_in_text(str(error))},
        )
