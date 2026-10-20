"""Per-domain browser cookie store with harvest helpers."""

from __future__ import annotations

import logging
from typing import Any

from app.models.normalized_url import NormalizedUrl
from app.transport.cookie_store import InMemoryCookieStore

logger = logging.getLogger(__name__)


__all__ = [
    "BROWSER_COOKIES_BY_DOMAIN",
    "BROWSER_COOKIES_MAX",
    "harvest_session_cookies",
    "store_browser_cookies",
]


BROWSER_COOKIES_MAX = 500


def _split_header(header: str) -> dict[str, str]:
    """The name->value mapping a coalesced HTTP ``Cookie`` header holds."""
    parsed: dict[str, str] = {}
    for part in header.split(";"):
        name, _, value = part.partition("=")
        if name.strip():
            parsed[name.strip()] = value.strip()
    return parsed


class _LockedDomainCookieStore:
    """Browser-harvest store: the in-memory medium at one implicit namespace."""

    __slots__ = ("_store", "_lock")

    def __init__(self, max_entries: int) -> None:
        self._store = InMemoryCookieStore(max_hosts_per_namespace=max_entries)
        # The single guard for every data op is the medium's own lock; this exposes
        # that same lock so the concurrency probe never double-acquires it.
        self._lock = self._store._lock

    def store(self, domain: str, header: str) -> None:
        self._store.put("", domain, _split_header(header))

    def touch_get(self, domain: str) -> str | None:
        cookies = self._store.load("", domain)
        if not cookies:
            return None
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


BROWSER_COOKIES_BY_DOMAIN = _LockedDomainCookieStore(BROWSER_COOKIES_MAX)


def reset_process_state() -> None:
    """Drop every cookie the renderer has accumulated for a domain.

    The store is process-wide and bounded, not cleared, so a browser test that
    renders once leaves its domain's cookies for the next test to assert against.
    """
    BROWSER_COOKIES_BY_DOMAIN.clear()


def has_process_state() -> bool:
    """Whether any domain's cookies are held right now."""
    return len(BROWSER_COOKIES_BY_DOMAIN) > 0


def store_browser_cookies(domain: str, header: str) -> None:
    """Record a harvested cookie header, evicting the LRU entry past the cap."""
    BROWSER_COOKIES_BY_DOMAIN.store(domain, header)


def get_browser_cookies_for(domain: str) -> str | None:
    return BROWSER_COOKIES_BY_DOMAIN.touch_get(domain)


async def harvest_session_cookies(page: Any, url: NormalizedUrl) -> None:
    """Capture the page's cookies before the context closes.

    Runs synchronously: the retry reads the store right after the render."""
    try:
        context = getattr(page, "context", None)
        if context is None:
            return
        cookies = await context.cookies(url.raw)
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
        if header:
            store_browser_cookies(url.site, header)
            logger.info("browser_cookies_harvested", extra={"domain": url.site, "count": len(cookies)})
    except Exception as error:
        logger.debug("browser_cookies_harvest_failed", extra={"error": str(error)})
