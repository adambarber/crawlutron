"""Owns namespaced persistent cookie storage and cookie context state.

This module owns cookie parsing, namespace keys, persistence, and the process cookie jar.
It must not construct HTTP clients or assemble request headers.
"""

from __future__ import annotations

import contextvars
import os
import threading
from collections import OrderedDict
from urllib.parse import urlparse

# Per-request cookie namespace: set by ``crawl_request`` so concurrent crawls
# from different tenants cannot leak cookies through the shared
# ``_GLOBAL_COOKIE_JAR``.
#
# IMPORTANT: the default namespace is ``""`` (empty string), which keeps the
# legacy single-jar behavior: every crawl that does NOT explicitly set
# ``request_profile.cookie_namespace`` shares the same jar. This is the
# sensible default for the common single-tenant deployment, but a
# multi-tenant deployment MUST set ``request_profile.cookie_namespace`` to a
# unique tenant id on every crawl, otherwise cookies WILL leak between
# tenants. The legacy shared behavior is intentionally NOT opt-in because
# silently switching to per-tenant isolation would break deployments that
# depend on cross-request cookie persistence for a single user
# (adversarial review F-cookie-cross-contamination).
_COOKIE_NAMESPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "browser_crawler_cookie_namespace", default=""
)

# Cap on distinct (namespace, host) cookie slots. The jar is module-global
# and long-lived: without eviction every crawled host accumulates forever
# (the DNS/robots/sitemap caches are all bounded; the jar was the outlier).
_COOKIE_JAR_MAX_HOSTS = 1024


class CookieJar:
    """In-memory cookie jar (Feature: Persistent Cookie Jars).

    Cookies are stored per-domain and merged into ``Set-Cookie`` response
    headers / ``Cookie`` request headers across recursive page loads. All
    access is guarded by a lock because the jar is shared across concurrent
    requests (a single global instance).

    A caller-supplied ``namespace`` string (e.g. a tenant id, a request id,
    or a logical ``cookie_jar`` profile name) is accepted by the public API
    and used to namespace stored cookies so concurrent calls from different
    namespaces cannot read each other's cookies. Passing ``namespace=""``
    (or omitting it) preserves the legacy shared behavior; the documented
    mitigation for multi-tenant deployments is to pass a per-tenant id.
    """

    def __init__(self) -> None:
        # OrderedDict: insertion order doubles as the eviction order for the
        # (namespace, host) LRU cap (R1) — plain dict has no move_to_end.
        self._cookies: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        # No per-namespace isolation is enforced here -- callers opt in by
        # passing a non-empty ``namespace``. The shared default is preserved
        # so existing single-tenant callers see no change.
        self._lock = threading.Lock()

    @staticmethod
    def _ns_key(namespace: str, host: str) -> tuple[str, str]:
        return (namespace or "", host)

    def add_cookie_header(self, url: str, set_cookie_header: str, *, namespace: str = "") -> None:
        """Parse a ``Set-Cookie`` header and store the cookie for the URL's domain.

        ``namespace`` isolates cookies between concurrent callers (e.g. per
        tenant id). An empty namespace shares the legacy global slot.
        """
        from http.cookies import SimpleCookie

        host = (urlparse(url).hostname or "").lower()
        jar = SimpleCookie()
        try:
            jar.load(set_cookie_header)
        except Exception:
            return
        ns_key = self._ns_key(namespace, host)
        with self._lock:
            target = self._cookies.setdefault(ns_key, {})
            for cookie in jar.values():
                target[cookie.key] = cookie.value
            # LRU: a stored host survives while it is written or read; past
            # the cap the oldest-unread slot is evicted (R1).
            self._cookies.move_to_end(ns_key)
            while len(self._cookies) > _COOKIE_JAR_MAX_HOSTS:
                self._cookies.popitem(last=False)

    def cookie_header_for(self, url: str, *, namespace: str = "") -> str:
        """Return a ``Cookie`` header string for the given URL's domain.

        ``namespace`` mirrors :meth:`add_cookie_header`. Empty namespace is
        the shared default and preserves single-tenant behavior.
        """
        host = (urlparse(url).hostname or "").lower()
        ns_key = self._ns_key(namespace, host)
        with self._lock:
            domain_cookies = dict(self._cookies.get(ns_key, {}))
            if ns_key in self._cookies:
                self._cookies.move_to_end(ns_key)
        if not domain_cookies:
            return ""
        return "; ".join(f"{key}={value}" for key, value in domain_cookies.items())

    def clear(self, *, namespace: str = "") -> None:
        """Clear cookies. With a ``namespace``, only that namespace is cleared."""
        with self._lock:
            if not namespace:
                self._cookies.clear()
                return
            stale = [key for key in self._cookies if key[0] == namespace]
            for key in stale:
                del self._cookies[key]

    def save_to_file(self, path: str) -> None:
        """Persist the cookie jar to a JSON file atomically.

        Routes through the app.atomic_write chokepoint (temp file + fsync
        + replace), so a crash (or write error) mid-serialization cannot
        leave a truncated/corrupted file that silently loses all cookies.
        """
        import json

        from app.atomic_write import atomic_write_text

        with self._lock:
            # Persist only the legacy shared namespace ("") for backward
            # compatibility; per-namespace cookies are caller-managed and
            # intentionally not persisted to disk.
            data: dict[str, dict[str, str]] = {}
            for (ns, host), cookies in self._cookies.items():
                if ns:
                    continue
                data[host] = dict(cookies)
        atomic_write_text(path, json.dumps(data))

    @classmethod
    def load_from_file(cls, path: str) -> CookieJar:
        """Load a cookie jar from a JSON file. Returns empty jar if file missing/invalid."""
        import json

        jar = cls()
        if not os.path.exists(path):
            return jar
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                with jar._lock:
                    for domain, cookies in data.items():
                        if isinstance(cookies, dict):
                            # Persisted cookies live in the legacy shared
                            # namespace ("") so single-tenant / pre-namespace
                            # deployments keep working. Per-namespace cookies
                            # are caller-managed and intentionally not
                            # persisted.
                            jar._cookies[("", domain)] = cookies
        except Exception:
            pass  # corrupted file -> empty jar
        return jar


# Module-level cookie jar for persistent cross-request cookie storage (#2).
_GLOBAL_COOKIE_JAR = CookieJar()
