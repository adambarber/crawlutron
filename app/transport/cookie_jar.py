"""Owns namespaced persistent cookie storage and cookie context state; it must not
construct HTTP clients or assemble headers. ``cookie_namespace`` is a PARTITIONING
convention, not authorization (no callers are authenticated); multi-tenant deployments add their own auth.
"""

from __future__ import annotations

import contextvars
import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from app.models.normalized_url import NormalizedUrl
from app.storage.coordination_cookies import DEFAULT_MAX_HOSTS_PER_NAMESPACE
from app.transport.cookie_store import InMemoryCookieStore

if TYPE_CHECKING:
    from app.storage.coordination_cookies import SharedCookieStore

# Per-request cookie namespace set by ``crawl_request``; its operator-facing
# limits live in the module docstring, not here.
COOKIE_NAMESPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "crawlutron_cookie_namespace", default=""
)

# Cap on distinct (namespace, host) cookie slots PER NAMESPACE: each tenant's
# budget is independent, so one crawl cannot evict another's. Kept on the store.
COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE = DEFAULT_MAX_HOSTS_PER_NAMESPACE


class CookieJar:
    """Cookie storage per-domain, optionally shared across worker processes, merged
    into Set-Cookie/Cookie headers across page loads. With a ``store`` the shared
    table is the single source of truth; a caller ``namespace`` isolates cookies between concurrent callers.
    """

    def __init__(self, store: SharedCookieStore | None = None) -> None:
        self._store = store
        # The process-local medium: per-namespace LRU over (namespace, host) slots.
        self._inmemory = InMemoryCookieStore(max_hosts_per_namespace=COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE)
        # The in-memory backing's mapping, exposed for lease/boundary probes; the
        # store object owns the lock discipline this jar applies around it.
        self._cookies: OrderedDict[tuple[str, str], dict[str, str]] = self._inmemory._data
        # No per-namespace isolation is enforced here: callers opt in with a
        # non-empty ``namespace``, and the shared default is preserved as-is.
        self._lock = threading.Lock()

    @property
    def store(self) -> SharedCookieStore | None:
        """The shared table this jar reads and writes, or None when it is process-local.
        Which of the two decides whether a cookie set by one worker is visible to
        another, so it is answerable without reaching into the instance.
        """
        return self._store

    @staticmethod
    def _ns_key(namespace: str, host: str) -> tuple[str, str]:
        return (namespace or "", host)

    def add_cookie_header(self, url: NormalizedUrl, set_cookie_header: str, *, namespace: str = "") -> None:
        """Parse a ``Set-Cookie`` header and store the cookie for the URL's domain.

        ``namespace`` isolates cookies between concurrent callers (e.g. per
        tenant id). An empty namespace shares the legacy global slot.
        """
        from http.cookies import SimpleCookie

        host = url.host
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
            self._inmemory.merge(ns_key[0], ns_key[1], parsed)

    def cookie_header_for(self, url: NormalizedUrl, *, namespace: str = "") -> str:
        """Return a ``Cookie`` header string for the given URL's domain.

        ``namespace`` mirrors :meth:`add_cookie_header`. Empty namespace is
        the shared default and preserves single-tenant behavior.
        """
        host = url.host
        ns_key = self._ns_key(namespace, host)
        if self._store is not None:
            domain_cookies = self._store.load(ns_key[0], ns_key[1])
        else:
            with self._lock:
                domain_cookies = self._inmemory.load(ns_key[0], ns_key[1])
        if not domain_cookies:
            return ""
        return "; ".join(f"{key}={value}" for key, value in domain_cookies.items())

    def clear(self, *, namespace: str = "") -> None:
        """Clear cookies. With a ``namespace``, only that namespace is cleared."""
        if self._store is not None:
            self._store.clear(namespace or "")
            return
        with self._lock:
            self._inmemory.clear(namespace or "")

    def export_to_file(self, path: str) -> None:
        """Write the jar's legacy shared namespace to a JSON file atomically --
        import/export, not live persistence (last-writer-wins is why a shared store
        exists) -- via the atomic_write chokepoint, so a crash cannot corrupt the file.
        """
        import json

        from app.safety.atomic_write import atomic_write_text

        atomic_write_text(path, json.dumps(self._legacy_slots()))

    def import_file_into(self, path: str) -> None:
        """Merge a legacy JSON jar's shared namespace into this jar; the file format
        is read here and nowhere else, so a store migration cannot drift from the
        export format. A missing or corrupt file is a no-op.
        """
        for host, cookies in self._read_legacy_file(path).items():
            if self._store is not None:
                self._store.merge("", host, cookies)
                continue
            with self._lock:
                self._inmemory.merge("", host, cookies)

    @classmethod
    def import_from_file(cls, path: str) -> CookieJar:
        """Load a JSON file into a memory-backed jar. Empty if missing/invalid."""
        jar = cls()
        jar.import_file_into(path)
        return jar

    def _legacy_slots(self) -> dict[str, dict[str, str]]:
        """The jar's shared namespace, in the legacy on-disk shape.
        Namespaced cookies stay in the store: caller-managed and never persisted,
        exporting them would leak one tenant's cookies into a file the next start imports.
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


def global_cookie_jar() -> CookieJar:
    """The process-wide jar shared with request profiles; swap for tests."""
    return _GLOBAL_COOKIE_JAR


def set_global_cookie_jar(jar: CookieJar) -> None:
    """Rebind the process-wide jar; the lifespan installs the disk-backed one."""
    global _GLOBAL_COOKIE_JAR
    _GLOBAL_COOKIE_JAR = jar


def reset_process_state() -> None:
    """Drop the process cookie jar and clear the per-request namespace.
    One door for one piece of state: the jar and the namespace contextvar are
    one unit, so resetting one without the other leaves a state production never reaches.
    """
    _GLOBAL_COOKIE_JAR.clear()
    COOKIE_NAMESPACE.set("")


def has_process_state() -> bool:
    """Whether the jar holds a cookie, or a request namespace is still selected."""
    # Not ``bool(_GLOBAL_COOKIE_JAR)``: the jar defines no ``__len__`` or
    # ``__bool__``, so truthiness is constant True; this reads its cookie mapping.
    return bool(_GLOBAL_COOKIE_JAR._cookies) or COOKIE_NAMESPACE.get() != ""
