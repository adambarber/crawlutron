"""Owns namespaced persistent cookie storage and cookie context state.

This module owns cookie parsing, namespace keys, persistence, and the process cookie jar.
It must not construct HTTP clients or assemble request headers.
"""

from __future__ import annotations

import contextvars
import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.crawl.coordination_cookies import DEFAULT_MAX_HOSTS

if TYPE_CHECKING:
    from app.crawl.coordination_cookies import SharedCookieStore

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
# depend on cross-request cookie persistence for a single user.
#
# READ THAT SENTENCE AS A REQUIREMENT ON THE OPERATOR, NOT AS A GUARANTEE.
# The namespace is a string the caller supplies in its own request profile, and
# this service authenticates no callers (``app/mcp/server.py`` states as much).
# So the namespace separates tenants only as far as callers choose distinct
# values and are trusted not to reuse each other's: a caller that names another
# tenant's namespace reads that tenant's cookies straight out of the shared
# module-global jar, and one that omits the key shares ``""`` with everyone.
# There is no configuration of this class that turns a self-asserted key into an
# authorization boundary; that would need a trusted caller identity the service
# does not have. Treat the namespace as a partitioning convention and put
# authentication in front of the service if isolation is required.
_COOKIE_NAMESPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "crawlutron_cookie_namespace", default=""
)

# Cap on distinct (namespace, host) cookie slots. The jar is module-global
# and long-lived: without eviction every crawled host accumulates forever
# (the DNS/robots/sitemap caches are all bounded; the jar was the outlier).
# Defined once by the shared store, which is what bounds the slots when the
# jar is storage-backed; re-declared here it would be a second number to drift.
_COOKIE_JAR_MAX_HOSTS = DEFAULT_MAX_HOSTS


class CookieJar:
    """Cookie storage, optionally shared by every worker process.

    Cookies are stored per-domain and merged into ``Set-Cookie`` response
    headers / ``Cookie`` request headers across recursive page loads.

    With a ``store`` the shared table is the single source of truth: the
    in-memory map stays empty, so there is no second copy to diverge. Without
    one the jar keeps cookies in a per-process LRU, which is a valid
    single-process configuration rather than a degraded one.

    A caller-supplied ``namespace`` string (e.g. a tenant id, a request id,
    or a logical ``cookie_jar`` profile name) is accepted by the public API
    and used to namespace stored cookies so concurrent calls from different
    namespaces cannot read each other's cookies. Passing ``namespace=""``
    (or omitting it) preserves the legacy shared behavior; the documented
    mitigation for multi-tenant deployments is to pass a per-tenant id.
    """

    def __init__(self, store: SharedCookieStore | None = None) -> None:
        self._store = store
        # OrderedDict: insertion order doubles as the eviction order for the
        # (namespace, host) LRU cap — plain dict has no move_to_end.
        self._cookies: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        # No per-namespace isolation is enforced here -- callers opt in by
        # passing a non-empty ``namespace``. The shared default is preserved
        # so existing single-tenant callers see no change.
        self._lock = threading.Lock()

    @staticmethod
    def _ns_key(namespace: str, host: str) -> tuple[str, str]:
        return (namespace or "", host)

    def _touch(self, ns_key: tuple[str, str]) -> None:
        """Mark `ns_key` most-recently-used and evict past the host cap.

        The LRU discipline in one place, called with the lock already held. It
        was written out at each of the two write paths -- the parsed
        ``Set-Cookie`` and the file import -- and the jar is module-global and
        long-lived, so a host cap that one of them forgot to apply is a slow leak
        that only a test watching the cap's bound would notice. A read counts as
        a use too, which is why this is named for the fact rather than the
        eviction.
        """
        self._cookies.move_to_end(ns_key)
        while len(self._cookies) > _COOKIE_JAR_MAX_HOSTS:
            self._cookies.popitem(last=False)

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
        parsed = {cookie.key: cookie.value for cookie in jar.values()}
        if not parsed:
            return
        ns_key = self._ns_key(namespace, host)
        if self._store is not None:
            self._store.merge(ns_key[0], ns_key[1], parsed)
            return
        with self._lock:
            target = self._cookies.setdefault(ns_key, {})
            target.update(parsed)
            self._touch(ns_key)

    def cookie_header_for(self, url: str, *, namespace: str = "") -> str:
        """Return a ``Cookie`` header string for the given URL's domain.

        ``namespace`` mirrors :meth:`add_cookie_header`. Empty namespace is
        the shared default and preserves single-tenant behavior.
        """
        host = (urlparse(url).hostname or "").lower()
        ns_key = self._ns_key(namespace, host)
        if self._store is not None:
            domain_cookies = self._store.get(ns_key[0], ns_key[1])
        else:
            with self._lock:
                domain_cookies = dict(self._cookies.get(ns_key, {}))
                if ns_key in self._cookies:
                    self._cookies.move_to_end(ns_key)
        if not domain_cookies:
            return ""
        return "; ".join(f"{key}={value}" for key, value in domain_cookies.items())

    def clear(self, *, namespace: str = "") -> None:
        """Clear cookies. With a ``namespace``, only that namespace is cleared."""
        if self._store is not None:
            self._store.clear(namespace or "")
            return
        with self._lock:
            if not namespace:
                self._cookies.clear()
                return
            stale = [key for key in self._cookies if key[0] == namespace]
            for key in stale:
                del self._cookies[key]

    def export_to_file(self, path: str) -> None:
        """Write the jar's legacy shared namespace to a JSON file atomically.

        Import/export, not live persistence. The shutdown save that used to
        live here was last-writer-wins across workers: it rewrote the whole
        file and discarded every sibling's cookies, which is why a shared store
        exists. A caller wanting a file now gets a mirror of shared state, and
        the mirror is never read back on the request path.

        Routes through the app.atomic_write chokepoint (temp file + fsync
        + replace), so a crash (or write error) mid-serialization cannot
        leave a truncated/corrupted file.
        """
        import json

        from app.atomic_write import atomic_write_text

        atomic_write_text(path, json.dumps(self._legacy_slots()))

    def import_file_into(self, path: str) -> None:
        """Merge a legacy JSON jar's shared namespace into this jar.

        The file format is read here and nowhere else, so a migration into a
        store-backed jar cannot drift from the format an export writes. A
        missing or corrupt file is a no-op: the caller keeps whatever it had.
        """
        for host, cookies in self._read_legacy_file(path).items():
            if self._store is not None:
                self._store.merge("", host, cookies)
                continue
            with self._lock:
                self._cookies.setdefault(("", host), {}).update(cookies)
                self._touch(("", host))

    @classmethod
    def import_from_file(cls, path: str) -> CookieJar:
        """Load a JSON file into a memory-backed jar. Empty if missing/invalid."""
        jar = cls()
        jar.import_file_into(path)
        return jar

    def _legacy_slots(self) -> dict[str, dict[str, str]]:
        """The jar's shared namespace, in the legacy on-disk shape.

        Namespaced cookies stay in the store: they are caller-managed and were
        never persisted, so exporting them would leak one tenant's cookies into
        a file the next start imports into the shared namespace.
        """
        if self._store is not None:
            slots = self._store.all_slots()
            return {host: cookies for (ns, host), cookies in slots.items() if not ns}
        with self._lock:
            return {host: dict(cookies) for (ns, host), cookies in self._cookies.items() if not ns}

    @staticmethod
    def _read_legacy_file(path: str) -> dict[str, dict[str, str]]:
        import json

        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}  # corrupted file -> nothing to import
        if not isinstance(data, dict):
            return {}
        return {
            domain: cookies
            for domain, cookies in data.items()
            if isinstance(domain, str) and isinstance(cookies, dict)
        }


# Module-level cookie jar for persistent cross-request cookie storage.
_GLOBAL_COOKIE_JAR = CookieJar()
