"""Per-domain browser cookie store with harvest helpers."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

from app.models.normalized_url import NormalizedUrl

logger = logging.getLogger(__name__)


# the shared classifier taxonomy, so HTTP and browser cannot disagree about a label.
_BROWSER_COOKIES_MAX = 500


class _LockedDomainCookieStore:
    """LRU cookie store whose every read and mutation is internally guarded.

    The store is module-global and genuinely concurrent: the render path writes
    harvested cookies while the crawl engine reads them for the cffi
    cookie-bootstrap retry, from different tasks and threads. Every operation
    acquires ``_lock`` for its whole duration, so an unsynchronized access is
    not a state a caller can construct and the eviction cannot interleave with a
    read.

    It implements no container protocol. It used to subclass
    ``MutableMapping``, and ten of its thirteen methods existed to honour that
    promise to callers outside ``app/`` -- no production path reached any of
    them, and ``no-unclaimed-container-protocol`` now holds that line.
    """

    __slots__ = ("_data", "_lock", "_max_entries")

    def __init__(self, max_entries: int) -> None:
        # OrderedDict: insertion order doubles as the eviction order (plain
        # dict has no move_to_end/popitem(last=False)).
        self._data: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def store(self, domain: str, header: str) -> None:
        """Record a harvested cookie header, evicting LRU entries past the cap.

        Assignment and eviction happen under one acquisition, so a concurrent
        reader can never observe the store over the cap, and the entry being
        written cannot be the one evicted by its own insert.
        """
        with self._lock:
            # Deliberately not move_to_end: re-storing a known domain updates
            # the value in place, matching plain-mapping semantics.
            self._data[domain] = header
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def touch_get(self, domain: str) -> str | None:
        """Look up a domain and mark it most-recently-used when present.

        Membership check, touch and value read are one critical section: this
        is the operation that used to race ``popitem`` eviction.
        """
        with self._lock:
            value = self._data.get(domain)
            if value is not None:
                self._data.move_to_end(domain)
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_BROWSER_COOKIES_BY_DOMAIN = _LockedDomainCookieStore(_BROWSER_COOKIES_MAX)


def reset_process_state() -> None:
    """Drop every cookie the renderer has accumulated for a domain.

    The store is process-wide and bounded, not cleared, so a browser test that
    renders once leaves its domain's cookies for the next test to assert against.
    """
    _BROWSER_COOKIES_BY_DOMAIN.clear()


def has_process_state() -> bool:
    """Whether any domain's cookies are held right now."""
    return len(_BROWSER_COOKIES_BY_DOMAIN) > 0


def _store_browser_cookies(domain: str, header: str) -> None:
    """Record a harvested cookie header, evicting the LRU entry past the cap."""
    _BROWSER_COOKIES_BY_DOMAIN.store(domain, header)


def get_browser_cookies_for(domain: str) -> str | None:
    return _BROWSER_COOKIES_BY_DOMAIN.touch_get(domain)


async def _harvest_session_cookies(page: Any, url: NormalizedUrl) -> None:
    """Capture the page's cookies before the context closes.

    Runs synchronously: the retry reads the store right after the render."""
    try:
        context = getattr(page, "context", None)
        if context is None:
            return
        cookies = await context.cookies(url.raw)
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
        if header:
            _store_browser_cookies(url.site, header)
            logger.info("browser_cookies_harvested", extra={"domain": url.site, "count": len(cookies)})
    except Exception as error:
        logger.debug("browser_cookies_harvest_failed", extra={"error": str(error)})
