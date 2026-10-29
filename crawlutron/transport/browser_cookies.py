"""Namespaced browser cookie store with harvest helpers."""

from __future__ import annotations

import logging

from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
from crawlutron.transport.cookie_store import InMemoryCookieStore
from crawlutron.transport.render_page import RenderPage

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


def _running_namespace(namespace: str | None) -> str:
    """The namespace a cookie op keys by: an explicit one, else the crawl's.

    ``None`` defers to ``COOKIE_NAMESPACE`` so the harvest and the retry land
    in the same caller's slot without a parameter chain through the worker.
    """
    if namespace is not None:
        return namespace
    return COOKIE_NAMESPACE.get()


class _LockedDomainCookieStore:
    """Browser-harvest store: the in-memory medium, keyed per caller namespace."""

    __slots__ = ("_store",)

    def __init__(self, max_entries: int) -> None:
        # The medium owns the lock discipline for every op; the wrapper holds no
        # lock of its own (no delegating-lock alias to deadlock against).
        self._store = InMemoryCookieStore(max_hosts_per_namespace=max_entries)

    def store(self, domain: str, header: str, namespace: str = "") -> None:
        self._store.put(namespace, domain, _split_header(header))

    def touch_get(self, domain: str, namespace: str = "") -> str | None:
        cookies = self._store.load(namespace, domain)
        if not cookies:
            return None
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


BROWSER_COOKIES_BY_DOMAIN = _LockedDomainCookieStore(BROWSER_COOKIES_MAX)


def reset_process_state() -> None:
    """Drop every cookie the renderer has accumulated, in every namespace.

    The store is process-wide and bounded, not cleared, so a browser test that
    renders once leaves its domain's cookies for the next test to assert against.
    """
    BROWSER_COOKIES_BY_DOMAIN.clear()


def has_process_state() -> bool:
    """Whether any namespace holds a cookie right now."""
    return len(BROWSER_COOKIES_BY_DOMAIN) > 0


def store_browser_cookies(domain: str, header: str, *, namespace: str | None = None) -> None:
    """Record a harvested cookie header, evicting the LRU entry past the cap.

    Keyed by the caller's namespace: one caller's harvested session
    never surfaces to another caller's jar.
    """
    BROWSER_COOKIES_BY_DOMAIN.store(domain, header, _running_namespace(namespace))


def get_browser_cookies_for(domain: str, *, namespace: str | None = None) -> str | None:
    """The harvested header for ``domain`` in the caller's namespace."""
    return BROWSER_COOKIES_BY_DOMAIN.touch_get(domain, _running_namespace(namespace))


async def harvest_session_cookies(
    page: RenderPage, url: NormalizedUrl, *, namespace: str | None = None
) -> None:
    """Capture the page's cookies before the context closes.

    Runs synchronously: the retry reads the store right after the render."""
    try:
        context = page.context
        cookies = await context.cookies(url.raw)
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
        if header:
            store_browser_cookies(url.site, header, namespace=namespace)
            logger.info("browser_cookies_harvested", extra={"domain": url.site, "count": len(cookies)})
    except Exception as error:
        logger.debug("browser_cookies_harvest_failed", extra={"error": str(error)})
