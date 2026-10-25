"""Owns namespaced persistent cookie storage and cookie context state; it must not
construct HTTP clients or assemble headers. ``cookie_namespace`` is a PARTITIONING
convention, not authorization (no callers are authenticated); multi-tenant deployments add their own auth.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
import urllib.request
from collections.abc import Callable, Iterator
from http import cookiejar as _cookiejar_module
from http.cookiejar import Cookie, DefaultCookiePolicy
from http.cookiejar import CookieJar as StdCookieJar
from typing import TYPE_CHECKING, TypedDict, cast

from app.models.normalized_url import NormalizedUrl
from app.models.registrable_domain import is_public_suffix
from app.storage.coordination_cookies import DEFAULT_MAX_HOSTS_PER_NAMESPACE
from app.storage.coordination_store import atwin
from app.transport.cookie_store import InMemoryCookieStore

if TYPE_CHECKING:
    from app.storage.coordination_cookies import SharedCookieStore

# typeshed omits this stdlib parser; vars() reads it without tripping the
# mypy attr check or B009, and the cast carries the type.
_parse_ns_headers = cast(
    "Callable[[list[str]], list[list[tuple[str, str | None]]]]",
    vars(_cookiejar_module)["parse_ns_headers"],
)

# Per-request cookie namespace set by ``crawl_request``; its operator-facing
# limits live in the module docstring, not here.
COOKIE_NAMESPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "crawlutron_cookie_namespace", default=""
)

# Cap on distinct (namespace, host) cookie slots PER NAMESPACE: each tenant's
# budget is independent, so one crawl cannot evict another's. Kept on the store.
COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE = DEFAULT_MAX_HOSTS_PER_NAMESPACE

# The tombstone reap window (#393): deletions propagate for this long, then
# their own writer prunes them. Same order as the DNS cache's 60s union window.
TOMBSTONE_WINDOW_SECONDS = 60.0


__all__ = [
    "CookieRecord",
    "CookieJar",
    "global_cookie_jar",
    "set_global_cookie_jar",
    "reset_process_state",
    "has_process_state",
    "sendable_cookie_header",
    "RecordingSendPolicy",
    "cookie_from",
    "COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE",
    "COOKIE_NAMESPACE",
    "MAX_COOKIE_LIFETIME_SECONDS",
    "TOMBSTONE_WINDOW_SECONDS",
]


class CookieRecord(TypedDict):
    """One cookie's RFC 6265 state; stored as the slot dict value's JSON."""

    value: str
    domain: str
    path: str
    secure: bool
    expires: float | None  # epoch seconds; None is a session cookie
    host_only: bool


_RECORD_KEYS = frozenset(CookieRecord.__annotations__)


class RecordingSendPolicy(DefaultCookiePolicy):
    """The send policy (RFC 6265, DomainStrict host-only pin, rfc2965 off) with a
    memory of what it accepted (#414): ``return_ok`` is the final send gate stdlib
    matching applies, so its acceptances are what the request carried.
    """

    def __init__(self) -> None:
        super().__init__(rfc2965=False, strict_ns_domain=DefaultCookiePolicy.DomainStrict)
        self.accepted: list[Cookie] = []

    def return_ok(self, cookie: Cookie, request: urllib.request.Request) -> bool:
        ok = super().return_ok(cookie, request)
        if ok:
            self.accepted.append(cookie)
        return ok


class CookieJar:
    """Cookie storage per-domain, optionally shared across worker processes, merged
    into Set-Cookie/Cookie headers across page loads. With a ``store`` the shared
    table is the single source of truth; a caller ``namespace`` isolates cookies between concurrent callers.
    """

    def __init__(self, store: SharedCookieStore | None = None) -> None:
        self._store = store
        # The process-local medium owns the lock discipline for every op; the
        # jar holds no lock of its own (one thread-safe guard, never two).
        self._inmemory = InMemoryCookieStore(max_hosts_per_namespace=COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE)
        # (namespace, slot host) -> {name: deleted_at epoch}: the tombstones
        # this jar wrote, pruned after the union window (#393).
        self._deleted: dict[tuple[str, str], dict[str, float]] = {}

    @property
    def store(self) -> SharedCookieStore | None:
        """The shared table this jar reads and writes, or None when it is process-local.
        Which of the two decides whether a cookie set by one worker is visible to
        another, so it is answerable without reaching into the instance.
        """
        return self._store

    def add_cookie_header(self, url: NormalizedUrl, set_cookie_header: str, *, namespace: str = "") -> None:
        """Parse one ``Set-Cookie`` header and store the cookie with its RFC 6265 attributes.

        ``namespace`` isolates concurrent callers; the slot is the cookie's own
        domain, so a Domain-scoped cookie travels with it and a deletion tombstone lands on the live entry.
        """
        parsed = _parse_set_cookie(set_cookie_header, url)
        if parsed is None:
            return
        name, record = parsed
        payload = {name: json.dumps(record)}
        key = (namespace or "", record["domain"])
        if record["value"] == "":
            self._deleted.setdefault(key, {})[name] = time.time()
        else:
            self._deleted.get(key, {}).pop(name, None)
        if self._store is not None:
            self._store.merge(key[0], key[1], payload)
            return
        self._inmemory.merge(key[0], key[1], payload)

    def cookie_header_for(self, url: NormalizedUrl, *, namespace: str = "") -> str:
        """Return a ``Cookie`` header for the given URL under RFC 6265 send rules.

        ``namespace`` mirrors :meth:`add_cookie_header`; empty is the shared default.
        Slots whose cookies the answer carried are marked recently used.
        """
        ns = namespace or ""
        records = self._records_for(ns)
        header, contributors = sendable_cookie_header(records, url)
        self._prune_deleted()
        medium = self._store if self._store is not None else self._inmemory
        for host in contributors:
            medium.touch(ns, host)
        return header

    def _records_for(self, namespace: str) -> list[tuple[str, str, CookieRecord]]:
        """Every live record in one namespace as (name, slot host, record)."""
        slots = self._store.all_slots() if self._store is not None else self._inmemory.snapshot()
        return list(_live_records(slots, namespace))

    # One async twin for the one door async crawl frames await (#141), derived
    # from the sync method: the fetch tails' Set-Cookie path never holds the loop.
    aadd_cookie_header = atwin(add_cookie_header)

    # The redirect re-attach's door, same declared direction: the cross-origin hop
    # reads the jar (SQLite under the store lock) from a fetcher's redirect loop.
    acookie_header_for = atwin(cookie_header_for)

    def _prune_deleted(self, *, now: float | None = None) -> None:
        """Reap this jar's tombstones older than TOMBSTONE_WINDOW_SECONDS (#393).

        The tombstone's own writer rewrites its slot minus dead records, scoped
        to the (namespace, host) slots holding them -- never another namespace.
        """
        if not self._deleted:
            return
        clock = time.time() if now is None else now
        medium = self._store if self._store is not None else self._inmemory
        for (ns, host), deleted in list(self._deleted.items()):
            expired = [n for n, at in deleted.items() if clock - at >= TOMBSTONE_WINDOW_SECONDS]
            if not expired:
                continue
            slot = medium.load(ns, host)
            if not slot:
                self._deleted.pop((ns, host), None)
                continue
            keep: dict[str, str] = {}
            changed = False
            for name, payload in slot.items():
                record = _load_record(payload)
                if record is not None and not _is_expired(record, clock):
                    keep[name] = payload
                else:
                    changed = True
            if changed:
                medium.put(ns, host, keep)
            for name in expired:
                deleted.pop(name, None)
            if not deleted:
                self._deleted.pop((ns, host), None)

    def clear(self, *, namespace: str = "") -> None:
        """Clear cookies. With a ``namespace``, only that namespace is cleared."""
        if self._store is not None:
            self._store.clear(namespace or "")
            return
        self._inmemory.clear(namespace or "")

    def export_to_file(self, path: str) -> None:
        """Write the jar's legacy shared namespace to a JSON file atomically --
        import/export, not live persistence (last-writer-wins is why a shared store
        exists) -- via the atomic_write chokepoint, so a crash cannot corrupt the file.
        """
        from app.safety.atomic_write import atomic_write_text

        atomic_write_text(path, json.dumps(self._legacy_slots()))

    def import_file_into(self, path: str) -> None:
        """Merge a legacy JSON jar's shared namespace into this jar; the file format
        is read here and nowhere else, so a store migration cannot drift from the
        export format. A missing or corrupt file is a no-op.
        """
        for host, cookies in self._read_legacy_file(path).items():
            records = {name: json.dumps(_host_cookie(host, value)) for name, value in cookies.items()}
            if self._store is not None:
                self._store.merge("", host, records)
                continue
            self._inmemory.merge("", host, records)

    @classmethod
    def import_from_file(cls, path: str) -> CookieJar:
        """Load a JSON file into a memory-backed jar. Empty if missing/invalid."""
        jar = cls()
        jar.import_file_into(path)
        return jar

    def _legacy_slots(self) -> dict[str, dict[str, str]]:
        """The jar's shared namespace, in the legacy on-disk shape.

        Live name=value pairs only: the legacy file never carried attributes,
        and a dead record must not resurrect through an export/import cycle.
        """
        slots = self._store.all_slots() if self._store is not None else self._inmemory.snapshot()
        out: dict[str, dict[str, str]] = {}
        for name, host, record in _live_records(slots, ""):
            out.setdefault(host, {})[name] = record["value"]
        return out

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


def _parse_set_cookie(header: str, url: NormalizedUrl) -> tuple[str, CookieRecord] | None:
    """Turn one Set-Cookie header into a (name, record) to store, or None.

    Tokenizing is stdlib ``parse_ns_headers``; the domain/path/expiry assembly
    pins RFC 6265 5.1/5.2/5.3, stated test by test in test_cookie_semantics.py.
    """
    pairs = _ns_pairs(header)
    if pairs is None:
        return None
    (name, value), *raw_attrs = pairs
    if not name or value is None:
        return None
    attrs = dict(raw_attrs)
    if (resolved := _cookie_domain(attrs, url)) is None:
        return None
    domain, host_only = resolved
    secure = "secure" in attrs
    if secure and url.scheme != "https":
        # RFC 6265bis 5.5: Secure cookies are refused over plain HTTP.
        return None
    path = str(attrs["path"]) if attrs.get("path") else _default_path(url)
    return name, CookieRecord(
        value=value, domain=domain, path=path, secure=secure, expires=_expiry(attrs), host_only=host_only
    )


def _ns_pairs(header: str) -> list[tuple[str, str | None]] | None:
    """The stdlib Netscape parse of one Set-Cookie header; None when unusable."""
    try:
        parsed = _parse_ns_headers([header])
    except Exception:
        return None
    if not parsed:
        return None
    pairs = parsed[0]
    return pairs if pairs else None


def _cookie_domain(attrs: dict[str, str | None], url: NormalizedUrl) -> tuple[str, bool] | None:
    """The storage domain with its host-only pin (5.3 steps 4-6), or None to refuse."""
    domain_attr = attrs.get("domain")
    if not domain_attr:
        return url.host.lower(), True
    domain = domain_attr.lstrip(".").lower()
    if is_public_suffix(domain):  # 6265bis 5.3: only the request host stays, host-only (#438)
        return (domain, True) if domain == url.host.lower() else None
    return (domain, False) if _domain_matches(url.host, domain) else None


def _domain_matches(host: str, domain: str) -> bool:
    """RFC 6265 5.1.3: string-equal, or a dot-suffix of a non-IP host."""
    host, domain = host.lower(), domain.lower()
    if host == domain:
        return True
    return host.endswith(f".{domain}") and not _is_ip_host(host)


def _is_ip_host(host: str) -> bool:
    """A dotted-numeric host is an IP address: suffix matching never applies."""
    return "." in host and host.replace(".", "").isdigit()


def _default_path(url: NormalizedUrl) -> str:
    """RFC 6265 5.1.4: the request path up to, but not including, the last '/'."""
    path = url.path or "/"
    if not path.startswith("/"):
        return "/"
    cut = path.rfind("/")
    return path[:cut] if cut > 0 else "/"


def _expiry(attrs: dict[str, str | None]) -> float | None:
    """RFC 6265 5.2.1/5.2.2: max-age wins; zero or a past date means now.
    A malformed max-age is ignored (the attribute does not apply), so a remote
    server cannot fail our fetch with a bad header value (#395)."""
    max_age = attrs.get("max-age")
    if isinstance(max_age, str):
        seconds = _max_age_seconds(max_age)
        if seconds is not None:
            return 0.0 if seconds <= 0 else _capped_expiry(time.time() + seconds)
    expires = attrs.get("expires")
    if isinstance(expires, (int, float)):
        return _capped_expiry(float(expires))
    return None


# RFC 6265bis 4.1.2.2/5.5 (#399): no cookie outlives this, whichever attribute set it.
MAX_COOKIE_LIFETIME_SECONDS = 400 * 24 * 60 * 60


def _capped_expiry(expiry: float) -> float:
    """The expiry the RFC allows: never past now + MAX_COOKIE_LIFETIME_SECONDS.

    A 40-digit Max-Age otherwise parses fine (Python integers are unbounded) and
    pins a cookie in the caller's jar for ever (#399)."""
    return min(expiry, time.time() + MAX_COOKIE_LIFETIME_SECONDS)


def _max_age_seconds(raw: str) -> int | None:
    """RFC 6265 5.2.2: the first character must be a DIGIT or '-'; anything else
    is non-conformant and ignored. Python integers are unbounded, so any digit
    string parses; the lifetime cap lives on ``_capped_expiry`` (#399)."""
    text = raw.strip()
    if not text or text[0] not in "-0123456789":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _is_expired(record: CookieRecord, now: float) -> bool:
    """RFC 6265 4.1.2.2: an expiry at or before now is dead (a tombstone)."""
    return record["expires"] is not None and record["expires"] <= now


def _live_records(
    slots: dict[tuple[str, str], dict[str, str]], namespace: str
) -> Iterator[tuple[str, str, CookieRecord]]:
    """Yield (name, slot host, record) for every live record in one namespace."""
    now = time.time()
    for (slot_ns, host), cookies in slots.items():
        if slot_ns != namespace:
            continue
        for name, payload in cookies.items():
            record = _load_record(payload)
            if record is not None and not _is_expired(record, now):
                yield name, host, record


def _load_record(payload: str) -> CookieRecord | None:
    """Decode one stored record; None when the value is not a record shape."""
    try:
        decoded: object = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(decoded, dict) or not _RECORD_KEYS <= decoded.keys():
        return None
    if not all(isinstance(decoded[key], str) for key in ("value", "domain", "path")):
        return None
    if not all(isinstance(decoded[key], bool) for key in ("secure", "host_only")):
        return None
    expires = decoded["expires"]
    if expires is not None and not isinstance(expires, (int, float)):
        return None
    return cast(CookieRecord, decoded)


def _host_cookie(host: str, value: str) -> CookieRecord:
    """A legacy file entry: name=value on the host itself, no attributes."""
    return CookieRecord(value=value, domain=host, path="/", secure=False, expires=None, host_only=True)


def sendable_cookie_header(
    records: list[tuple[str, str, CookieRecord]], url: NormalizedUrl
) -> tuple[str, set[str]]:
    """The Cookie header RFC 6265 5.4 allows for ``url``, longest path first.

    Matching and ordering delegate to stdlib http.cookiejar; contributors (#414)
    come back as the policy's acceptances, not header-carried names.
    """
    policy = RecordingSendPolicy()
    jar = StdCookieJar(policy=policy)
    slots: dict[int, str] = {}
    for name, slot, record in sorted(records, key=lambda item: -len(item[2]["path"])):
        cookie = cookie_from(name, record)
        slots[id(cookie)] = slot
        jar.set_cookie(cookie)
    # Header-carrier for cookiejar matching only; never opened.
    request = urllib.request.Request(url.raw)  # noqa: S310
    jar.add_cookie_header(request)
    header = request.get_header("Cookie") or ""
    return header, {slots[id(cookie)] for cookie in policy.accepted}


def cookie_from(name: str, record: CookieRecord) -> Cookie:
    """The stdlib Cookie shape for one record, mirroring its attributes."""
    domain_specified = not record["host_only"]
    return Cookie(
        version=0,
        name=name,
        value=record["value"],
        port=None,
        port_specified=False,
        domain=record["domain"],
        domain_specified=domain_specified,
        domain_initial_dot=domain_specified,
        path=record["path"],
        path_specified=True,
        secure=record["secure"],
        expires=int(record["expires"]) if record["expires"] is not None else None,
        discard=record["expires"] is None,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


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
    # Rebuild, not merely clear: a swapped-in double must not stay installed
    # after the reset -- the engine's reset door rebuilds its objects the same way.
    global _GLOBAL_COOKIE_JAR
    _GLOBAL_COOKIE_JAR = CookieJar()
    COOKIE_NAMESPACE.set("")


def has_process_state() -> bool:
    """Whether the jar holds a cookie, or a request namespace is still selected."""
    # Not ``bool(_GLOBAL_COOKIE_JAR)``: the jar defines no ``__len__`` or
    # ``__bool__``, so truthiness is constant True; ask the medium instead.
    return len(_GLOBAL_COOKIE_JAR._inmemory) > 0 or COOKIE_NAMESPACE.get() != ""
