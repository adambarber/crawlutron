"""HTTP fetch strategy: direct + curl_cffi impersonation, conditional caching."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import inspect
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
import base64
from dataclasses import asdict
from typing import Any

import httpx

try:
    from charset_normalizer import from_bytes as _charset_from_bytes
except ImportError:
    _charset_from_bytes = None  # type: ignore[assignment]

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

from app.classify import classify_page_with_evidence
from app.constants import (
    CFFI_IMPERSONATE_TARGET,
    DEFAULT_EXTRACTION,
    DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_USER_AGENT,
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    HTTP_BLOCKED_CLASSIFICATIONS,
    HTTP_CFFI_IMPERSONATE,
    SUCCESS_CLASSIFICATIONS,
)
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.document_text import (
    _format_extracted_document_text,
)


def _serialize_response(resp: Any) -> dict[str, Any]:
    """Capture a fetcher response into a JSON-serializable envelope."""
    return {
        "status_code": int(resp.status_code),
        "headers": dict(resp.headers),
        "body_b64": base64.b64encode(resp.body).decode("ascii"),
        "final_url": str(resp.final_url),
        "elapsed_ms": int(getattr(resp, "elapsed_ms", 0)),
    }


def _rebuild_response(data: dict[str, Any]) -> Any:
    """Reconstruct a minimal response object from a cached envelope."""
    class _CachedResponse:
        pass

    r = _CachedResponse()
    r.status_code = data["status_code"]
    r.headers = data["headers"]
    r.body = base64.b64decode(data["body_b64"])
    r.final_url = data["final_url"]
    r.elapsed_ms = data.get("elapsed_ms", 0)
    return r

from urllib.parse import urljoin

from app.crawl.decompression import DecompressionTooLargeError, detect_decompression_failure
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.extraction import (
    _detect_pagination,
    _extract_discovered_links,
    _extract_document,
    _extract_html,
    _html_diagnostics,
    _parse_html_tree,
    extract_content,
    extract_js_endpoints,
)
from app.crawl.result_helpers import _binary_no_text_result, _make_engine_result
from app.models import CrawlStrategyAttempt, EngineCrawlResult, HttpFetchResult


def _classify_error_code(exc: BaseException) -> str:
    """Map an internal exception to a stable, non-leaking error code.

    Raw exception text can leak DNS hostnames, internal filesystem paths, or
    stack detail. Callers must never see ``str(exc)``; these codes are safe to
    surface in user-facing ``error``/``diagnostics`` fields.
    """
    if isinstance(exc, DecompressionTooLargeError):
        return "payload_too_large"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.ProtocolError,
            socket.gaierror,
            socket.herror,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            TimeoutError,
            asyncio.TimeoutError,
        ),
    ):
        return "connection_error"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status_error"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    if isinstance(exc, ValueError):
        return "value_error"
    return "network_error"



# Shared HTTP/2 connection-pooled async client (one per running event loop).
_DEFAULT_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_INIT_LOCK = threading.Lock()  # serializes first-time client construction


def _default_settings() -> CrawlerSettings:
    """Get default crawler settings for HTTP client configuration."""
    from app.settings import CrawlerSettings
    return CrawlerSettings.from_mapping()


def _build_async_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with configurable connection pool."""
    if settings is None:
        settings = _default_settings()
    client = httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=30.0,
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
            keepalive_expiry=settings.http_keepalive_expiry,
        ),
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        client._bound_loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    except RuntimeError:
        client._bound_loop = None  # type: ignore[attr-defined]
    return client


async def _safe_aclose(client):
    """Await the client\'s async close, swallowing failures.

    The client is being replaced (its event loop changed); any close error is
    irrelevant, so we ignore it rather than risk surfacing during shutdown.
    """
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass


# Superseded pooled clients awaiting a live event loop to be closed on.
# Bounded so a stream of superseded clients cannot grow the list without
# bound if drains are delayed (adversarial review F-pending-client-close).
_PENDING_CLIENT_CLOSE: list[httpx.AsyncClient] = []
_PENDING_CLIENT_CLOSE_MAX = 32
_PENDING_CLIENT_CLOSE_LOCK = threading.Lock()


async def _drain_pending_client_closes() -> None:
    """Close any superseded pooled clients on the current (live) loop.

    ``_schedule_client_close`` fires on the *dying* loop, where the scheduled
    task is cancelled before ``aclose`` completes (every ``asyncio.run`` exit).
    Draining here, inside a fetch that is already running on a live loop,
    actually releases those sockets instead of leaking them. A bounded
    queue with cap-on-enqueue keeps the list from growing unboundedly under
    a stream of rapid loop changes (adversarial F-pending-client-close).
    """
    while True:
        with _PENDING_CLIENT_CLOSE_LOCK:
            if not _PENDING_CLIENT_CLOSE:
                return
            client = _PENDING_CLIENT_CLOSE.pop(0)
        await _safe_aclose(client)


def _schedule_client_close(client) -> None:
    """Best-effort asynchronous close of a superseded pooled HTTP client.

    ``get_async_http_client`` runs within an event loop, so we schedule the
    async ``aclose`` on the running loop instead of blocking. This prevents
    leaking the old client\'s sockets/file descriptors when the loop changes
    (e.g. repeated ``asyncio.run`` calls).
    """
    if client is None or getattr(client, "is_closed", True):
        return
    try:
        asyncio.ensure_future(_safe_aclose(client))
    except RuntimeError:
        # No running loop available to schedule on; nothing safe to do.
        pass


def get_async_http_client(settings: CrawlerSettings | None = None) -> httpx.AsyncClient:
    """Return the pooled ``httpx.AsyncClient`` bound to the current event loop.

    The client is reused while the running event loop is unchanged (HTTP/2
    connection pooling), and transparently rebuilt if the loop changes (for
    example when a test harness drives many ``asyncio.run`` calls in sequence).
    """
    global _DEFAULT_HTTP_CLIENT
    if settings is None:
        settings = _default_settings()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    # Guard construction so concurrent first-time callers cannot both build a
    # client and leak one (the httpx client is reused across requests).
    with _HTTP_CLIENT_INIT_LOCK:
        if _DEFAULT_HTTP_CLIENT is None or _DEFAULT_HTTP_CLIENT.is_closed:
            _DEFAULT_HTTP_CLIENT = _build_async_client(settings)
        elif getattr(_DEFAULT_HTTP_CLIENT, "_bound_loop", None) is not loop:
            old_client = _DEFAULT_HTTP_CLIENT
            _DEFAULT_HTTP_CLIENT = _build_async_client(settings)
            # Close the superseded client to avoid leaking its sockets/fds:
            # queue it for a guaranteed close on the next live-loop fetch, and
            # also try the immediate best-effort schedule.
            stale_to_release: object | None = None
            with _PENDING_CLIENT_CLOSE_LOCK:
                # Dedupe check inside the cap branch so a re-entrant
                # append+pop race cannot pop two distinct entries
                # (adversarial review F-pending-client-close B3).
                already_queued = old_client in _PENDING_CLIENT_CLOSE
                if not already_queued:
                    if len(_PENDING_CLIENT_CLOSE) >= _PENDING_CLIENT_CLOSE_MAX:
                        # Cap reached: drop the oldest pending reference and
                        # release the lock BEFORE calling sync ``close()`` so
                        # the (potentially slow) socket-close path cannot
                        # block other clients trying to enter this critical
                        # section (adversarial review F-pending-client-close
                        # B-minor lock-held-during-close).
                        stale_to_release = _PENDING_CLIENT_CLOSE.pop(0)
                    _PENDING_CLIENT_CLOSE.append(old_client)
            # Outside the lock: ``httpx.AsyncClient.close()`` is synchronous
            # and releases the underlying httpcore connection pool without
            # awaiting the async teardown path. We can release sockets even
            # from sync contexts and from inside a dying loop. Falling back
            # to a no-op when ``close`` raises (e.g. on a half-constructed
            # client) lets GC reclaim the rest
            # (adversarial review F-pending-client-close B1/B2).
            if stale_to_release is not None:
                try:
                    stale_to_release.close()
                except Exception:  # noqa: BLE001
                    pass
            _schedule_client_close(old_client)
    return _DEFAULT_HTTP_CLIENT


async def close_async_http_client() -> None:
    """Close the shared async client on service shutdown."""
    global _DEFAULT_HTTP_CLIENT
    await _drain_pending_client_closes()
    client = _DEFAULT_HTTP_CLIENT
    _DEFAULT_HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


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
        self._cookies: dict[str, dict[str, dict[str, str]]] = {}
        # No per-namespace isolation is enforced here -- callers opt in by
        # passing a non-empty ``namespace``. The shared default is preserved
        # so existing single-tenant callers see no change.
        self._lock = threading.Lock()

    @staticmethod
    def _ns_key(namespace: str, host: str) -> tuple[str, str]:
        return (namespace or "", host)

    def add_cookie_header(
        self, url: str, set_cookie_header: str, *, namespace: str = ""
    ) -> None:
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

    def cookie_header_for(self, url: str, *, namespace: str = "") -> str:
        """Return a ``Cookie`` header string for the given URL's domain.

        ``namespace`` mirrors :meth:`add_cookie_header`. Empty namespace is
        the shared default and preserves single-tenant behavior.
        """
        host = (urlparse(url).hostname or "").lower()
        ns_key = self._ns_key(namespace, host)
        with self._lock:
            domain_cookies = dict(self._cookies.get(ns_key, {}))
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

        Writes to a temp file in the same directory and ``os.replace``s it into
        place, so a crash (or write error) mid-serialization cannot leave a
        truncated/corrupted file that silently loses all persisted cookies.
        """
        import json
        import os

        with self._lock:
            # Persist only the legacy shared namespace ("") for backward
            # compatibility; per-namespace cookies are caller-managed and
            # intentionally not persisted to disk.
            data: dict[str, dict[str, str]] = {}
            for (ns, host), cookies in self._cookies.items():
                if ns:
                    continue
                data[host] = dict(cookies)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        directory = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".cookiejar.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @classmethod
    def load_from_file(cls, path: str) -> CookieJar:
        """Load a cookie jar from a JSON file. Returns empty jar if file missing/invalid."""
        import json
        import os

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


def _is_private_or_loopback_ip(ip: str) -> bool:
    """Return True if ``ip`` is NOT a safe public address (SSRF protection).

    Rejects private, loopback, link-local, reserved, multicast and unspecified
    ranges, plus carrier-grade NAT (100.64.0.0/10), IPv6 unique-local
    (fc00::/7) and NAT64 (64:ff9b::/96) which older stdlib ``is_private`` does
    not always cover (F7).
    """
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    # Carrier-grade NAT (RFC 6598) - shared ISP space, not a public target.
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True
    # IPv6 unique-local (RFC 4193) is functionally private.
    if isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network("fc00::/7"):
        return True
    # NAT64 (RFC 6147) well-known prefix.
    if isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network("64:ff9b::/96"):
        return True
    return False


# Per-host DNS resolution cache (#3). Crawling a single host thousands of times
# would otherwise issue a blocking socket.getaddrinfo per fetch. The TTL is
# deliberately short so DNS changes (and the rebinding window for SSRF) stay
# bounded; the SSRF path re-validates resolved IPs on every fresh resolution.
_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL = 60.0
_DNS_CACHE_MAX = 4096
# Guard for concurrent reads/writes to _DNS_CACHE. _resolve_ips is called from
# both the async event loop and worker threads (asyncio.to_thread) so the cache
# mutation path needs explicit synchronization -- a plain dict.get/dict[]
# sequence can corrupt state or drop entries under concurrent access
# (adversarial review F-thread-safety).
_DNS_CACHE_LOCK = threading.Lock()


def _resolve_ips(hostname: str, *, use_cache: bool = True) -> list[str]:
    """Resolve a hostname to its IPs with a short TTL cache.

    Returns the raw IP list from socket.getaddrinfo (may include IPv6). Raises
    socket.gaierror on resolution failure.

    Caching avoids a blocking resolver call on every fetch in high-concurrency
    batches. The pinning path (``_resolve_and_pin_host``) passes
    ``use_cache=False`` so the connection always pins a *freshly* resolved,
    freshly re-validated public IP -- a cached pin would let a DNS rebind
    inside the TTL window bypass the SSRF check (TOCTOU). The validation-only
    path (``_check_ssrf_protection``) may use the cache: it only needs to know
    the host is safe to connect to, and the actual connection re-validates.
    """
    if use_cache:
        now = time.monotonic()
        with _DNS_CACHE_LOCK:
            cached = _DNS_CACHE.get(hostname)
        if cached and (now - cached[0]) < _DNS_TTL:
            return cached[1]
    addrinfo = socket.getaddrinfo(hostname, None)
    ips = [entry[4][0] for entry in addrinfo]
    if use_cache:
        # Only the validation path populates the cache. The pinning path
        # resolves fresh and must never persist an IP here -- otherwise a
        # private/rebound IP seen during pinning would poison the shared
        # validation cache and let the check later treat it as safe.
        with _DNS_CACHE_LOCK:
            _DNS_CACHE[hostname] = (time.monotonic(), ips)
            if len(_DNS_CACHE) > _DNS_CACHE_MAX:
                # Soft cap: drop the whole cache rather than letting it grow
                # unbounded across a crawl that touches many unique hosts. A brief
                # re-resolution burst is acceptable and keeps memory predictable.
                _DNS_CACHE.clear()
    return ips


def _check_ssrf_protection(url: str) -> None:
    """Resolve the URL hostname and abort if any IP is private/loopback.

    Set ``BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1`` (or ``true``/``yes``/``on``)
    to disable for local testing.
    """
    from app.validation import parse_bool_env
    if parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False):
        return
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return
    try:
        ips = _resolve_ips(hostname)
    except socket.gaierror:
        # An unresolvable hostname is not a valid public target; fail closed
        # rather than silently allowing it. Previously this let unresolvable
        # hostnames bypass the private-IP block and reach loopback via the pin
        # fallback in _resolve_and_pin_host.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}")
    for ip in ips:
        if _is_private_or_loopback_ip(ip):
            raise ValueError(f"SSRF protection: hostname {hostname} resolves to private/loopback IP {ip}")


def _resolve_and_pin_host(url: str) -> tuple[str, str]:
    """Resolve the hostname once and pin the resulting IP for the connection.

    Prevents DNS-rebinding (TOCTOU) SSRF: ``_check_ssrf_protection`` validates
    the resolved IPs, then this helper returns the FIRST public IP so the HTTP
    client connects to that exact IP instead of re-resolving at connect time.
    The original ``Host`` header is preserved by the caller.
    """
    import os
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return hostname or "", hostname or ""
    from app.validation import parse_bool_env
    if parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False):
        # Local-dev escape hatch: bypass resolution but connect to the REAL
        # hostname. Previously this hardcoded 127.0.0.1, which misrouted every
        # fetch/webhook to loopback and broke virtual-host routing.
        return hostname, hostname
    try:
        # Resolve FRESH (no cache): the pinned IP governs the actual connection
        # and must be re-validated against the SSRF blocklist on every call so a
        # DNS rebind inside the cache TTL cannot bypass the check (TOCTOU).
        ips = _resolve_ips(hostname, use_cache=False)
    except socket.gaierror:
        # Do NOT fall back to loopback. Returning the original hostname makes
        # the HTTP client attempt resolution itself and fail with a connection
        # error instead of silently connecting to 127.0.0.1 (SSRF bypass, F9).
        return hostname, hostname
    for ip in ips:
        if not _is_private_or_loopback_ip(ip):
            return hostname, ip
    # No public IP found (should have been caught above) -> fail safe
    raise ValueError(f"SSRF protection: hostname {hostname} has no public IP")


def _http_413(url: str, response: Any, started: float) -> HttpFetchResult:
    """Standard 413 (payload too large) result for size/cap violations."""
    return HttpFetchResult(
        url=url, final_url=str(response.url), status_code=413,
        headers={"content-type": "application/octet-stream"},
        body=b"", elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _build_pinned_request(url: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Resolve + validate the target (DNS-rebinding safe) and build the request.

    Returns ``(request_url, request_headers)`` shared by both fetchers.

    The target's DNS is resolved once and the resulting IP is validated against
    the SSRF blocklist *before* connecting. For plain HTTP the connection is
    additionally pinned to that exact IP. For HTTPS the original URL is kept:
    TLS SNI/hostname verification is performed against the real hostname
    (CDN/edge certificates do not carry IP SANs, so rewriting the URL to the IP
    breaks every properly-configured HTTPS site). The rebinding window on HTTPS
    is therefore bounded by CA validation plus the pre-connect IP check instead
    of raw pinning.
    """
    _check_ssrf_protection(url)
    parsed = urlparse(url)
    if parsed.scheme == "https":
        # Validate resolved IPs, but keep the hostname for correct TLS.
        _resolve_and_pin_host(url)
        return url, dict(headers)
    hostname, pinned_ip = _resolve_and_pin_host(url)
    pinned_url = (
        f"{parsed.scheme}://{pinned_ip}:{parsed.port or 80}"
        f"{parsed.path or '/'}"
        + (f"?{parsed.query}" if parsed.query else "")
    )
    request_headers = dict(headers)
    if parsed.hostname:
        request_headers["Host"] = parsed.hostname
    return pinned_url, request_headers


# Dedicated thread pool for blocking DNS resolution so hundreds of concurrent
# fetches never block the event loop or contend with the default asyncio executor
# (used for extraction and other blocking work). The pinning path resolves FRESH
# on every call for SSRF safety, so without this the loop would stall on each
# resolver call (adversarial perf #1.1).
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="dns-resolve"
)


async def _build_pinned_request_async(url: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Offload SSRF-safe request building (incl. the blocking ``socket.getaddrinfo``
    call) to a worker thread so the event loop is never stalled by a resolver
    lookup (adversarial perf #1.1). Mirrors the browser route handler, which
    already runs ``_build_pinned_request`` via ``asyncio.to_thread``.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DNS_EXECUTOR, _build_pinned_request, url, headers)


_MAX_REDIRECT_HOPS = 5

_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


# Headers that must never survive a cross-host redirect hop.
_CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})


def _redirect_target(
    current_url: str,
    request_headers: dict[str, str],
    status_code: int,
    location: str | None,
    hops: int,
) -> tuple[str, dict[str, str]] | None:
    """Decide the next hop of a manual redirect loop (shared by both fetchers).

    Returns ``(new_url, updated_headers)`` when the response is an in-budget
    redirect, else ``None`` (terminal response). The caller must re-run
    ``_build_pinned_request`` for ``new_url`` so every hop re-validates against
    the SSRF blocklist and re-pins DNS (F1). Sensitive headers (Cookie,
    Authorization) are stripped when crossing host boundaries (Fix 3).
    """
    if status_code not in _REDIRECT_STATUSES or not location or hops >= _MAX_REDIRECT_HOPS:
        return None
    new_url = urljoin(current_url, location)
    new_host = urlparse(new_url).hostname
    current_host = urlparse(current_url).hostname
    if new_host != current_host:
        request_headers = {
            k: v for k, v in request_headers.items()
            if k.lower() not in _CREDENTIAL_HEADERS
        }
    return new_url, request_headers


async def _default_http_fetcher(url: str, headers: dict[str, str], timeout_seconds: int, max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES) -> HttpFetchResult:
    """Async HTTP/2 fetch using the shared pooled ``httpx.AsyncClient`` with
    granular timeouts (#4), SSRF protection (#7), content-length limits (#5) and
    SSRF-safe manual redirect handling (F1).

    Redirects are followed by hand so every hop is re-validated against the SSRF
    blocklist and re-pinned to its resolved IP. Automatic httpx following is off
    because it would connect to a redirect target without re-checking it.
    Sensitive headers (Cookie, Authorization) are stripped when crossing host
    boundaries to prevent credential leakage (Fix 3).
    """
    await _drain_pending_client_closes()
    client = get_async_http_client()
    started = time.monotonic()
    timeout = httpx.Timeout(connect=5.0, read=float(timeout_seconds), write=10.0, pool=5.0)
    current_url = url
    hops = 0
    # Work with a copy of headers so we can mutate per-hop
    request_headers = dict(headers)
    while True:
        pinned_url, hop_headers = await _build_pinned_request_async(current_url, request_headers)
        try:
            # follow_redirects=False: we follow manually so each hop is SSRF-checked.
            async with client.stream("GET", pinned_url, headers=hop_headers, timeout=timeout, follow_redirects=False) as response:
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_content_length:
                    return _http_413(url, response, started)
                status_code = response.status_code
                location = response.headers.get("location")
                # SSRF-safe redirect: only follow within the hop budget. The next
                # loop iteration re-runs _build_pinned_request, which re-validates
                # (and re-pins) the redirect target against the SSRF blocklist.
                redirect = _redirect_target(current_url, request_headers, status_code, location, hops)
                if redirect is not None:
                    current_url, request_headers = redirect
                    hops += 1
                    continue
                # Terminal response: stream RAW bytes, capping total read (#5).
                # bytearray.extend is O(chunk); ``raw += chunk`` on immutable
                # bytes re-allocates and copies the whole buffer per chunk.
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    raw.extend(chunk)
                    if len(raw) > max_content_length:
                        return _http_413(url, response, started)
                encoding = response.headers.get("content-encoding", "")
                try:
                    body = _decompress_body(raw, encoding, max_output_bytes=max_content_length)
                except DecompressionTooLargeError:
                    return _http_413(url, response, started)
                resp_headers = {key.lower(): value for key, value in response.headers.items()}
                # Already decompressed above; drop header to avoid a second pass.
                resp_headers.pop("content-encoding", None)
                final_url = str(response.url)
        except httpx.HTTPStatusError as error:
            response = error.response
            body = response.content
            status_code = response.status_code
            resp_headers = {key.lower(): value for key, value in response.headers.items()}
            final_url = str(response.url)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _store_set_cookies(final_url or url, resp_headers)
        return HttpFetchResult(
            url=url,
            final_url=final_url,
            status_code=status_code,
            headers=resp_headers,
            body=body,
            elapsed_ms=elapsed_ms,
        )


async def _default_cffi_fetcher(url: str, headers: dict[str, str], timeout_seconds: int, max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES) -> HttpFetchResult:
    """TLS-fingerprint-impersonated async HTTP fetcher using curl_cffi.

    Mirrors _default_http_fetcher, including SSRF-safe manual redirect handling
    (F1) and sensitive-header stripping on cross-domain redirects (Fix 3).
    Falls back to plain httpx on any import/fetch failure.
    """
    try:
        import curl_cffi.requests as cffi_requests
    except ImportError:
        return await _default_http_fetcher(url, headers, timeout_seconds, max_content_length)
    try:
        # NOTE: the client class is AsyncSession (curl_cffi>=0.7 API).
        # AsyncClient does not exist; referencing it raises AttributeError
        # and would silently degrade every impersonated fetch to plain httpx.
        async with cffi_requests.AsyncSession(impersonate=CFFI_IMPERSONATE_TARGET, timeout=float(timeout_seconds)) as client:
            current_url = url
            hops = 0
            started = time.monotonic()
            # Work with a copy of headers so we can mutate per-hop
            request_headers = dict(headers)
            while True:
                pinned_url, hop_headers = await _build_pinned_request_async(current_url, request_headers)
                try:
                    response = await client.get(pinned_url, headers=hop_headers, allow_redirects=False, stream=True)
                except Exception:
                    # Fall back to plain httpx for this request on transport errors.
                    return await _default_http_fetcher(url, headers, timeout_seconds, max_content_length)
                # Content-length check (#5)
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_content_length:
                    return HttpFetchResult(
                        url=url, final_url=str(response.url), status_code=413,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=b"", elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                status_code = response.status_code
                location = response.headers.get("location")
                # SSRF-safe redirect: only follow within the hop budget; the next
                # iteration re-validates + re-pins the target via _build_pinned_request.
                redirect = _redirect_target(current_url, request_headers, status_code, location, hops)
                if redirect is not None:
                    current_url, request_headers = redirect
                    hops += 1
                    continue
                elapsed_ms = int(getattr(response, "elapsed", 0) * 1000) if hasattr(response, "elapsed") else 0
                # Stream RAW bytes with a running cap (H1): a hostile origin
                # can omit content-length and stream gigabytes (chunked).
                # Abort with a 413 as soon as the cap is exceeded instead of
                # buffering the whole body into memory first.
                encoding = response.headers.get("content-encoding", "")
                raw = bytearray()
                async for chunk in response.aiter_content():
                    raw.extend(chunk)
                    if len(raw) > max_content_length:
                        return HttpFetchResult(
                            url=url, final_url=str(response.url), status_code=413,
                            headers={key.lower(): value for key, value in response.headers.items()},
                            body=b"", elapsed_ms=elapsed_ms,
                        )
                raw_bytes = bytes(raw)
                # Decompress with a size cap (decompression-bomb guard).
                try:
                    body = _decompress_body(raw_bytes, encoding, max_output_bytes=max_content_length)
                except DecompressionTooLargeError:
                    return HttpFetchResult(
                        url=url, final_url=str(response.url), status_code=413,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=b"", elapsed_ms=elapsed_ms,
                    )
                cffi_decompression_failed = detect_decompression_failure(raw_bytes, encoding, body)
                resp_headers = {key.lower(): value for key, value in response.headers.items()}
                resp_headers.pop("content-encoding", None)
                _store_set_cookies(str(response.url), response.headers)
                return HttpFetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=status_code,
                    headers=resp_headers,
                    body=body,
                    elapsed_ms=elapsed_ms,
                    decompression_failed=cffi_decompression_failed,
                )
    except Exception:  # any cffi failure falls back to plain httpx
        return await _default_http_fetcher(url, headers, timeout_seconds, max_content_length)


def cookie_namespace_for(profile: dict[str, Any]) -> str:
    """Return a per-request cookie namespace, or ``""`` for the shared default.

    Callers running multi-tenant crawls should set
    ``request_profile.cookie_namespace`` to a tenant id so cookies from
    different callers cannot leak into each other's sessions. Empty / unset
    preserves the legacy shared behavior so single-tenant callers and the
    test suite see no change.
    """
    value = profile.get("cookie_namespace")
    return str(value).strip() if isinstance(value, str) else ""


def _cookie_header_for_url(profile: dict[str, Any], url: str) -> str:
    """Build a Cookie header from policy cookies and the persistent jar (#2)."""
    host = (urlparse(url).hostname or "").lower()
    parts: list[str] = []

    # Static policy cookies
    if profile.get("cookie_jar") in {"per_host", "session"}:
        cookies = profile.get("cookies") or {}
        if isinstance(cookies, dict):
            host_cookies = cookies.get(host) or {}
            if isinstance(host_cookies, dict):
                parts.extend(f"{key}={value}" for key, value in host_cookies.items())

    # Persistent cookies from prior responses (optionally namespaced via the
    # profile or the per-request cookie namespace contextvar).
    namespace = cookie_namespace_for(profile) or _COOKIE_NAMESPACE.get()
    jar_header = _GLOBAL_COOKIE_JAR.cookie_header_for(url, namespace=namespace)
    if jar_header:
        parts.append(jar_header)

    return "; ".join(parts)


def _store_set_cookies(
    url: str,
    response_headers: httpx.Headers | dict[str, str],
    *,
    namespace: str | None = None,
) -> None:
    """Extract Set-Cookie headers from a response into the persistent jar (#2).

    Uses ``get_list`` to capture all ``Set-Cookie`` headers (httpx 0.28+
    returns comma-joined values from ``.items()`` for duplicate keys).
    ``namespace`` isolates cookies for multi-tenant callers; when not
    provided, the per-request cookie namespace contextvar is used.
    """
    if namespace is None:
        namespace = _COOKIE_NAMESPACE.get()
    # Accept both httpx.Headers (for get_list) and plain dict (for tests)
    if hasattr(response_headers, "get_list"):
        set_cookies = response_headers.get_list("set-cookie")
    else:
        # Fallback for plain dict (test mocks) - iterate items
        set_cookies = [v for k, v in response_headers.items() if k.lower() == "set-cookie"]
    for cookie in set_cookies:
        _GLOBAL_COOKIE_JAR.add_cookie_header(url, cookie, namespace=namespace)


_BUILTIN_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]
_BUILTIN_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "fr-FR,fr;q=0.9",
    "de-DE,de;q=0.9",
    "es-ES,es;q=0.9",
]


def _domain_seed(url: str) -> str:
    """Hostname used as the rotation seed (stable; avoids normalization side effects)."""
    return urlparse(url).hostname or ""


def _rotating_index(profile: dict[str, Any], domain: str, pool_size: int) -> int:
    """Deterministic per-host pool index so repeated crawls of the same host keep a
    stable fingerprint (the disk content-cache key stays consistent) while different
    hosts rotate through the pool (#11)."""
    seed = f"{profile.get('fingerprint_seed', '')}:{domain}".encode()
    return int(hashlib.sha256(seed).hexdigest(), 16) % pool_size


def _select_fingerprint(profile: dict[str, Any], domain: str) -> dict[str, str]:
    """Select fingerprint signals for a host (#11). Rotation is deterministic per host.
    Explicit ``user_agents``/``accept_languages`` pools are preferred; ``fingerprint_rotation``
    falls back to built-in pools. Anchored on the host so it is complementary to Camoufox
    and curl_cffi TLS impersonation rather than conflicting with them."""
    out: dict[str, str] = {}
    ua_pool = list(profile.get("user_agents") or [])
    if not ua_pool and profile.get("fingerprint_rotation"):
        ua_pool = _BUILTIN_USER_AGENTS
    if ua_pool:
        out["user_agent"] = ua_pool[_rotating_index(profile, domain, len(ua_pool))]
    al_pool = list(profile.get("accept_languages") or [])
    if not al_pool and profile.get("fingerprint_rotation"):
        al_pool = _BUILTIN_ACCEPT_LANGUAGES
    if al_pool:
        out["accept_language"] = al_pool[_rotating_index(profile, domain, len(al_pool))]
    return out


def _headers_for_strategy(strategy: str, policy: dict[str, Any], url: str) -> dict[str, str]:
    profile = policy.get("request_profile") or {}
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    user_agent_family = str(profile.get("user_agent_family", "generic_crawler"))
    is_browser_like = strategy == HTTP_CFFI_IMPERSONATE or user_agent_family == "desktop_chrome"
    # Fingerprint injection/rotation (#11): deterministic per-host selection so a single
    # crawl rotates fingerprints across hosts while staying stable per host.
    fp = _select_fingerprint(profile, _domain_seed(url))
    if "user_agent" in fp:
        headers["User-Agent"] = fp["user_agent"]
        is_browser_like = is_browser_like or "Mozilla" in fp["user_agent"]
    elif is_browser_like:
        # Firefox identity: must match CFFI_IMPERSONATE_TARGET AND the Camoufox
        # browser strategy so cookie-bootstrap sessions carry a consistent
        # User-Agent across strategies.
        if strategy == HTTP_CFFI_IMPERSONATE:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
                "Gecko/20100101 Firefox/135.0"
            )
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            headers["Referer"] = "https://www.google.com/"
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = "cross-site"
            headers["Sec-Fetch-User"] = "?1"
        else:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            headers["Referer"] = "https://www.google.com/"
            headers["Sec-Fetch-Site"] = "cross-site"
            headers["Sec-CH-UA"] = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
            headers["Sec-CH-UA-Mobile"] = "?0"
            headers["Sec-CH-UA-Platform"] = '"Windows"'
            headers["Sec-Fetch-User"] = "?1"
    elif user_agent_family == "mobile_safari":
        headers["User-Agent"] = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
    if "accept_language" in fp:
        headers["Accept-Language"] = fp["accept_language"]
    cookie_header = _cookie_header_for_url(profile, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    headers.update(policy["headers"])
    return headers


def _decode_body(body: bytes | bytearray | memoryview, content_type: str | None, headers: dict[str, str] | None = None) -> str:
    """Decode a response body to text.

    Try the declared (or UTF-8) charset strictly first; fall back to
    charset-normalizer when that fails, then to a lossy UTF-8 decode. This
    avoids scanning/rewriting the whole body with ``errors='replace'`` (and
    the resulting replacement-character check) on the common valid-UTF-8 path
    (#5).

    ``body`` may be a bytearray/memoryview when the streamed fetcher returns an
    uncompressed buffer without converting to immutable bytes; ``bytes`` is
    required for the ``.decode()`` calls below, so normalize here exactly once.
    """
    if not isinstance(body, bytes):
        body = bytes(body)
    if headers:
        body = _decompress_body(body, headers.get("content-encoding", ""))

    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip()

    # Fast path: strict decode with the declared (or default UTF-8) charset.
    try:
        return body.decode(charset)
    except (UnicodeDecodeError, LookupError):
        pass

    # Fallback to charset-normalizer if the strict decode failed.
    if _charset_from_bytes is not None:
        detected = _charset_from_bytes(body)
        best = detected.best()
        if best is not None and best.encoding:
            return str(best)

    # Final fallback: lossy UTF-8 decode so we always return a string.
    return body.decode("utf-8", errors="replace")


def _status_for_classification(classification: str) -> str:
    if classification in SUCCESS_CLASSIFICATIONS:
        return "success"
    if classification in HTTP_BLOCKED_CLASSIFICATIONS:
        return "blocked"
    if classification == "not_modified":
        return "success"
    return "failed"


def _quality_score(status: str, classification: str, text: str | None, html: str | None, http_status: int | None) -> float:
    if status != "success" or classification != "rendered":
        return 0.0
    text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    normalized_html = (html or "").lower()
    has_frameset = "<frameset" in normalized_html or "<frame" in normalized_html
    score = 0.45
    if http_status and 200 <= http_status < 300:
        score += 0.2
    if text_length >= 20:
        score += 0.25
    elif text_length > 0:
        score += 0.15
    if html_length >= 40:
        score += 0.05
    if has_frameset and text_length < 40:
        score = min(score, 0.4)
    return min(score, 1.0)


# Single compiled pattern so _content_metrics counts every tag of interest
# in ONE linear scan of the HTML instead of four separate re.findall/re.search
# passes (#13). Each group is tallied as we walk the string once.
_CONTENT_TAG_RE = re.compile(r"<(a|iframe|frame|frameset|form)\b", re.IGNORECASE)


def _content_metrics(text: str | None, html: str | None) -> dict[str, int | float | bool]:
    visible_text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    counts = {"a": 0, "iframe": 0, "frame": 0, "frameset": 0, "form": 0}
    for match in _CONTENT_TAG_RE.finditer(html or ""):
        counts[match.group(1).lower()] += 1
    return {
        "visible_text_length": visible_text_length,
        "html_length": html_length,
        "link_count": counts["a"],
        # frame_count historically combines <iframe> and <frame>.
        "frame_count": counts["iframe"] + counts["frame"],
        "form_count": counts["form"],
        "has_frameset": counts["frameset"] > 0,
        "text_to_html_ratio": round(visible_text_length / html_length, 4) if html_length else 0.0,
    }


def _retry_after_seconds(headers: dict[str, str]) -> int | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        # Clamp hostile values: an unbounded Retry-After would park scheduler
        # coroutines in cooldown sleep for years (adversarial review C2/C3).
        return min(300, max(0, int(value.strip())))
    except ValueError:
        return None


async def _fetch_with_retries(
    fetcher: Any,
    url: str,
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
        except Exception as error:  # noqa: BLE001
            transient_errors.append(_classify_error_code(error))
            if attempt >= max_attempts:
                raise


def _looks_like_binary_body(body: bytes, content_type: str | None, url: str) -> bool:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("text/") or normalized_content_type in {"application/json", "application/xml", "application/xhtml+xml"}:
        return False
    if "html" in normalized_content_type or "pdf" in normalized_content_type:
        return False
    lower_url = url.split("?", 1)[0].lower()
    if lower_url.endswith((".zip", ".gz", ".tgz", ".7z", ".rar", ".xlsx", ".docx")):
        return True
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b")):
        return True
    return normalized_content_type in {"application/octet-stream", "application/zip", "application/x-zip-compressed"}



async def _discovery_diagnostics(
    *,
    normalized_url: str,
    html: str | None,
    policy: dict[str, Any],
    headers: dict[str, str],
    http_fetcher: Any,
    engine_robots_sitemaps: list[str] | None = None,
) -> dict[str, Any]:
    discovery = policy.get("discovery") or {}
    limits = policy.get("limits", {})
    diagnostics: dict[str, Any] = {}
    parsed = urlparse(normalized_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_sitemaps: list[str] = []
    if engine_robots_sitemaps is not None:
        # The engine already fetched /robots.txt (for allow/deny checks) and
        # extracted its Sitemap directives; reuse them instead of hitting the
        # network a second time (#4). An empty list is a valid signal (robots
        # fetched, no Sitemap lines) and still means "do not re-fetch".
        robots_sitemaps = list(engine_robots_sitemaps)
        diagnostics["robots"] = {
            "url": None,
            "allowed": True,
            "crawl_delay_seconds": None,
            "sitemaps": robots_sitemaps,
            "reused_from_engine": True,
        }
    elif discovery.get("robots"):
        robots_url = urljoin(origin, "/robots.txt")
        try:
            robots_response = http_fetcher(robots_url, headers, limits.get("timeout_seconds", 10))
            if inspect.iscoroutine(robots_response):
                robots_response = await robots_response
            robots_headers = {key.lower(): value for key, value in robots_response.headers.items()}
            robots_text = _decode_body(_decompress_body(robots_response.body, robots_headers.get("content-encoding", "")), robots_headers.get("content-type"))
            crawl_delay = None
            for line in robots_text.splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "crawl-delay":
                    try:
                        crawl_delay = int(value.strip())
                    except ValueError:
                        crawl_delay = None
                elif key.strip().lower() == "sitemap":
                    robots_sitemaps.append(value.strip())
            diagnostics["robots"] = {
                "url": robots_url,
                "allowed": True,
                "crawl_delay_seconds": crawl_delay,
                "sitemaps": robots_sitemaps,
            }
        except Exception as error:  # noqa: BLE001
            diagnostics["robots"] = {"url": robots_url, "error": _classify_error_code(error), "sitemaps": []}
    sitemap_urls: list[str] = []
    if discovery.get("sitemaps"):
        candidate_sitemaps = robots_sitemaps or ["/sitemap.xml"]
        from app.crawl.fetch_helpers import fetch_sitemap_urls
        for sitemap_url in (urljoin(origin, entry) for entry in candidate_sitemaps):
            try:
                # Recursively resolves sitemap indexes down to (loc, lastmod) pairs.
                found_locs = await fetch_sitemap_urls(http_fetcher, sitemap_url, limits.get("timeout_seconds", 10))
                for loc, lastmod in found_locs:
                    sitemap_urls.append(loc)
                    if lastmod:
                        diagnostics.setdefault("sitemap_lastmod", {})[loc] = lastmod
            except Exception:
                continue
    feeds: list[str] = []
    if discovery.get("feeds"):
        for match in re.finditer(r"<link\b[^>]*rel=['\"][^'\"]*alternate[^'\"]*['\"][^>]*>", html or "", flags=re.IGNORECASE):
            tag = match.group(0)
            if "rss" not in tag.lower() and "atom" not in tag.lower():
                continue
            href_match = re.search(r"href=['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
            if href_match:
                feeds.append(urljoin(normalized_url, href_match.group(1)))
    if sitemap_urls or feeds:
        diagnostics["discovery"] = {"sitemap_urls": sitemap_urls, "feeds": feeds}
    return diagnostics


def _content_cache_key(
    normalized_url: str,
    strategy: str,
    base_headers: dict[str, str],
    extraction: str | None,
    css_selector: str | None,
    xpath: str | None,
) -> str:
    """Stable content-cache key mirroring what http.py stores under.

    Kept as a shared helper so engine.py can reconstruct the exact key a future
    fetch of ``normalized_url`` would use (for sitemap lastmod freshness checks,
    #3) without duplicating the key composition.
    """
    return ContentCache.make_key(
        "http",
        normalized_url,
        strategy,
        json.dumps(sorted(base_headers.items()), separators=(",", ":")),
        extraction,
        css_selector,
        xpath,
    )


# Adversarial perf #2.1: bound the RAM consumed by in-flight page bodies. At high
# global_concurrency the per-page cap shrinks so the whole active set cannot
# exhaust the budget. Operators with known RAM headroom can override the budget.
_RAM_SAFETY_BYTES = int(
    os.environ.get("BROWSER_CRAWLER_CONTENT_RAM_BUDGET_BYTES", str(1 * 1024 * 1024 * 1024))
)
_MIN_PER_PAGE_BYTES = 256 * 1024  # never shrink the cap below 256 KiB


def _default_global_concurrency() -> int:
    from app.settings import CrawlerSettings

    try:
        return max(1, int(CrawlerSettings.from_mapping().global_concurrency))
    except Exception:  # pragma: no cover - settings should always parse
        return 4


def _effective_max_content_length(
    limits: dict[str, Any], global_concurrency: int | None = None
) -> int:
    """Per-page body cap, scaled down as concurrency rises (adversarial perf #2.1).

    An explicit ``max_content_length_bytes`` in the policy always wins. Otherwise
    the cap is ``min(DEFAULT, RAM_BUDGET / concurrency)`` so the in-flight page
    set stays within budget even when hundreds of pages run concurrently. At the
    default concurrency (4) this resolves to the unchanged 10 MiB default.
    """
    explicit = limits.get("max_content_length_bytes")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return int(explicit)
    conc = global_concurrency or _default_global_concurrency()
    scaled = min(
        DEFAULT_MAX_CONTENT_LENGTH_BYTES,
        max(_MIN_PER_PAGE_BYTES, _RAM_SAFETY_BYTES // max(conc, 1)),
    )
    return int(scaled)


async def _http_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    policy: dict[str, Any],
    http_fetcher: Any,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    caller_validators: dict[str, str] | None = None,
    robots_sitemaps: list[str] | None = None,
) -> EngineCrawlResult:
    started = time.monotonic()
    retry_diagnostics: dict[str, Any] = {"attempts": 0, "transient_errors": []}
    # Caller-supplied conditional validators (freshness monitoring): sent as
    # If-None-Match / If-Modified-Since so an unchanged page returns 304.
    decompression_failed = False
    caller_conditional: dict[str, str] = {}
    if caller_validators:
        if caller_validators.get("if_none_match"):
            caller_conditional["If-None-Match"] = caller_validators["if_none_match"]
        if caller_validators.get("if_modified_since"):
            caller_conditional["If-Modified-Since"] = caller_validators["if_modified_since"]
    # Apply policy limits; fall back to strategy defaults when the caller
    # omitted them (engine-provided policies always carry explicit limits).
    limits = policy.get("limits", {})
    timeout_seconds = limits.get("timeout_seconds", 10)
    max_retries = limits.get("max_retries", 0)
    max_content_length = _effective_max_content_length(limits)
    base_headers = _headers_for_strategy(strategy, policy, normalized_url)
    request_headers = dict(base_headers)
    if cache is not None:
        request_headers.update(cache.conditional_headers(normalized_url))
    if caller_conditional:
        request_headers.update(caller_conditional)

    extraction = policy.get("extraction", DEFAULT_EXTRACTION)
    # Drop the raw HTML as early as possible when the caller did not ask for it
    # so the decoded string + lxml tree are not retained in the result (T3/#2.1).
    include_html = bool((policy.get("browser_profile") or {}).get("include_html", True))

    # Disk content cache (#5): serve an unchanged URL from local disk instead of
    # re-downloading. The key is derived from the *base* request headers (excluding
    # HTTP conditional-revalidation headers) so it stays stable across crawls -- the
    # conditional headers only change the 304 handshake, never the payload.
    ckey: str | None = None
    content_hit = False
    revalidated_304 = False
    if content_cache is not None:
        ckey = _content_cache_key(
            normalized_url, strategy, base_headers, extraction,
            policy.get("css_selector"), policy.get("xpath"),
        )
        cached = await content_cache.aget(ckey)
        if cached is not None:
            validator = cache.conditional_headers(normalized_url) if cache is not None else {}
            if not validator and caller_conditional:
                # Caller-supplied validators must win over serving a cached body:
                # silently ignoring If-None-Match serves stale content as success.
                validator = dict(caller_conditional)
            if not validator:
                # No validator to re-check against -> serve cached payload directly.
                # Staleness is bounded by the cache TTL (acceptable for etag-less pages).
                response = _rebuild_response(cached)
                content_hit = True
                retry_diagnostics = {"attempts": 0, "transient_errors": [], "content_cache": "hit"}
            else:
                # A validator exists -> revalidate with a cheap conditional GET so we
                # never serve stale content for an etagged/last-modified URL.
                reval_headers = dict(base_headers)
                reval_headers.update(validator)
                reval_headers.update(caller_conditional)
                response, retry_diagnostics = await _fetch_with_retries(
                    http_fetcher, normalized_url, reval_headers,
                    timeout_seconds, max_retries, max_content_length,
                )
                if response.status_code == 304:
                    response = _rebuild_response(cached)
                    retry_diagnostics = {**retry_diagnostics, "content_cache": "hit_revalidated"}
                    revalidated_304 = True
                # otherwise the response is fresh and falls through to be stored below.

    try:
        if not content_hit and not revalidated_304:
            response, retry_diagnostics = await _fetch_with_retries(
                http_fetcher,
                normalized_url,
                request_headers,
                timeout_seconds,
                max_retries,
                max_content_length,
            )

        # HTTP conditional caching: a 304 from a real fetch means the page is unchanged.
        # (A revalidation-304 already had its cached body restored above, so it must
        #  NOT be reported as not_modified -- we serve the cached content instead.)
        # Caller-supplied validators (freshness monitoring) also produce a
        # not_modified result on 304: no body exists for caller-owned validators.
        # A 304 we did not negotiate (no If-* header was sent) is honored by the
        # letter of HTTP but is trivially attacker-controlled content suppression;
        # treat it as a normal response body instead.
        resp_headers_304 = {key.lower(): value for key, value in response.headers.items()}
        echoed_validators = {
            "etag": resp_headers_304.get("etag") or (caller_conditional.get("If-None-Match") if caller_conditional else None),
            "last_modified": resp_headers_304.get("last-modified") or (caller_conditional.get("If-Modified-Since") if caller_conditional else None),
        }
        echoed_validators = {k: v for k, v in echoed_validators.items() if v}
        sent_conditional = any(
            key in request_headers for key in ("If-None-Match", "If-Modified-Since")
        )
        if response.status_code == 304 and not revalidated_304 and sent_conditional:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempt = CrawlStrategyAttempt(
                strategy=strategy,  # type: ignore[arg-type]
                status="success",
                classification="not_modified",
                http_status=304,
                content_type=None,
                elapsed_ms=elapsed_ms,
                error=None,
            )
            diagnostics_304: dict[str, Any] = {"retry": retry_diagnostics}
            if cache is not None:
                diagnostics_304["cache"] = "not_modified"
            if caller_conditional:
                diagnostics_304["conditional"] = dict(caller_conditional)
            return _make_engine_result(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=response.final_url,
                status="success",
                classification="not_modified",
                selected_strategy=strategy,  # type: ignore[arg-type]
                attempt=attempt,
                http_status=304,
                headers=response.headers,
                diagnostics=diagnostics_304,
                error=None,
                validators=echoed_validators,
            )

        if cache is not None:
            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")
            if etag or last_modified:
                cache.store(normalized_url, dict(response.headers))

        if content_cache is not None and ckey is not None and not content_hit:
            if response.status_code < 300 and response.status_code != 304:
                try:
                    await content_cache.aset(ckey, _serialize_response(response))
                except Exception:  # noqa: BLE001
                    pass

        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type")
        if policy.get("extraction") != EXTRACTION_DOCUMENT and _looks_like_binary_body(response.body, content_type, response.final_url):
            result = _binary_no_text_result(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=response.final_url,
                strategy=strategy,
                http_status=response.status_code,
                headers=headers,
                content_type=content_type,
                elapsed_ms=response.elapsed_ms,
            )
            return EngineCrawlResult(**{**asdict(result), "diagnostics": {"retry": retry_diagnostics}})
        content_encoding_header = headers.get("content-encoding", "")
        if content_encoding_header:
            # The fetcher normally decompresses and strips this header; if it is
            # still present the body was not verified -- detect silent failure.
            decompressed = _decompress_body(response.body, content_encoding_header)
            strategy_decompression_failed = detect_decompression_failure(
                response.body, content_encoding_header, decompressed
            )
        else:
            decompressed = response.body
            strategy_decompression_failed = bool(getattr(response, "decompression_failed", False))
        body_text = _decode_body(decompressed, content_type)
        is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(("<html", "<!doctype html"))
        html = body_text if is_html else None
        if extraction == EXTRACTION_DOCUMENT:
            extracted = await asyncio.to_thread(_extract_document, decompressed, response.final_url)
            text = extracted.text or ""
            markdown = extracted.markdown
            if (content_type or "").lower().startswith("text/"):
                text = _format_extracted_document_text(text, source_url=response.final_url)
                markdown = text
            extraction_method = EXTRACTION_METHOD_DOCUMENT
        elif extraction == EXTRACTION_TRAFILATURA:
            if is_html:
                extracted = _extract_html(body_text, response.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
                text = extracted.text or ""
                markdown = extracted.markdown
            else:
                text = ""
                markdown = None
            extraction_method = EXTRACTION_METHOD_TRAFILATURA
        else:
            extracted = await asyncio.to_thread(extract_content, decompressed, content_type, response.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
            text = extracted.text or ""
            markdown = extracted.markdown
            extraction_method = EXTRACTION_METHOD_HTTP_HTML if is_html else EXTRACTION_METHOD_HTTP_BODY
        classification, classification_evidence = classify_page_with_evidence(html, text)
        if response.status_code == 403 and classification == "rendered":
            classification = "access_denied"
            classification_evidence = [{"classification": "access_denied", "signature": "403", "source": "http_status", "context": "HTTP 403"}]
        elif response.status_code == 404:
            classification = "not_found"
            classification_evidence = [{"classification": "not_found", "signature": "404", "source": "http_status", "context": "HTTP 404"}]
        elif response.status_code == 410:
            classification = "gone"
            classification_evidence = [{"classification": "gone", "signature": "410", "source": "http_status", "context": "HTTP 410"}]
        elif response.status_code == 429:
            classification = "rate_limited"
            classification_evidence = [{"classification": "rate_limited", "signature": "429", "source": "http_status", "context": "HTTP 429"}]
        elif response.status_code >= 500:
            classification = "site_error"
            classification_evidence = [{"classification": "site_error", "signature": str(response.status_code), "source": "http_status", "context": f"HTTP {response.status_code}"}]
        if extraction == EXTRACTION_TRAFILATURA and not is_html and classification == "rendered":
            classification = EXTRACTION_METHOD_BINARY_NO_TEXT
            classification_evidence = [{"classification": EXTRACTION_METHOD_BINARY_NO_TEXT, "signature": "non_html_body", "source": "content_type", "context": content_type or ""}]
        status = _status_for_classification(classification)
        diagnostics: dict[str, Any] = {
            "decompression_failed": bool(strategy_decompression_failed or getattr(response, "decompression_failed", False)),
            "request_profile": {
                "user_agent_family": str(policy.get("request_profile", {}).get("user_agent_family", "generic_crawler")),
                "cookie_jar": str(policy.get("request_profile", {}).get("cookie_jar", "none")),
            },
            "retry": retry_diagnostics,
            "runtime": {"elapsed_ms": response.elapsed_ms, "final_url": response.final_url},
            "content_metrics": _content_metrics(text, html),
        }
        # Discovery is only requested when the caller opts in; recursive crawls
        # (max_depth > 0) also need discovered links to expand the frontier.
        discovery = policy.get("discovery") or {}
        _max_depth = int((policy.get("limits") or {}).get("max_depth") or 0)
        _want_discovery_links = bool(discovery) or _max_depth > 0
        # #11: parse the page HTML at most once and reuse the single lxml tree
        # for the frames/forms diagnostics and the link extraction below.
        # Only parse when discovery is requested or this is a recursive crawl
        # that expands the frontier from the extracted links (B1).
        tree = _parse_html_tree(html) if _want_discovery_links and html else None
        # #2: _html_diagnostics (frames/forms via an lxml parse) is only needed
        # for structural discovery. Skip it when discovery is not requested so
        # content-only crawls avoid a redundant HTML parse per page.
        if discovery:
            diagnostics.update(_html_diagnostics(html, response.final_url, tree=tree))
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            diagnostics["cooldown"] = {"retry_after_seconds": retry_after, "scope": "domain"}
        # Sitemap/feed discovery is only performed when discovery is requested
        # (the /v1/map path forces discovery, so maps are unaffected). Skipping
        # it for content-only crawls avoids the discovery fetches (B1).
        if discovery:
            diagnostics.update(
                await _discovery_diagnostics(
                    normalized_url=normalized_url,
                    engine_robots_sitemaps=robots_sitemaps,
                    html=html,
                    policy=policy,
                    # Base headers only: page-level If-None-Match /
                    # If-Modified-Since must never reach robots.txt or
                    # sitemap fetches (false 304s / missed sitemaps).
                    headers=base_headers,
                    http_fetcher=http_fetcher,
                )
            )
        if _max_depth > 0:
            # Site-wide discovery lists are frontier seeds, not per-page
            # payload: returning them on every page of a recursive crawl
            # wastes memory across thousands of pages (#13-fix).
            discovery_diag = diagnostics.get("discovery")
            if discovery_diag:
                discovery_diag.pop("sitemap_urls", None)
                discovery_diag.pop("feeds", None)
        # Emitted discovery URLs are attacker-influenced: only http(s) targets
        # may leave the service (no javascript:, file:, link-local oddities).
        # JS-endpoint extraction is discovery-oriented; skip it for
        # content-only crawls (B1).
        raw_endpoints = extract_js_endpoints(html, normalized_url) if discovery else []
        diagnostics["js_endpoints"] = [
            endpoint
            for endpoint in raw_endpoints
            if urlparse(str(endpoint.get("url", ""))).scheme in ("", "http", "https")
        ]
        # Pagination detection stays on for all crawls: next_page_url is a
        # standard result field (returned even without discovery mode), so it is
        # always computed. Only the heavier discovery-only extras below are
        # gated on `discovery` (B1).
        next_page_url = _detect_pagination(html, response.final_url)
        if next_page_url and urlparse(next_page_url).scheme not in ("http", "https"):
            next_page_url = None
        if next_page_url:
            diagnostics["next_page_url"] = next_page_url
        quality = _quality_score(status, classification, text, html, response.status_code)
        # Surface origin validators so callers can revalidate cheaply later.
        if strategy_decompression_failed or getattr(response, "decompression_failed", False):
            # Garbage compressed bytes must not masquerade as high-quality content.
            quality = min(quality, 0.25)
        origin_validators = {
            key: value
            for key, value in (
                ("etag", headers.get("etag")),
                ("last_modified", headers.get("last-modified")),
            )
            if value
        }
        if (
            status == "success"
            and not strategy_decompression_failed
            and diagnostics["content_metrics"]["visible_text_length"] >= 40
        ):
            # Boost only trustworthy content: garbage from failed decompression
            # can easily exceed the length threshold.
            quality = max(quality, 0.95)
        # discovered_links is consumed by frontier expansion and by discovery
        # consumers; content-only crawls (no discovery, depth 0) skip the
        # extraction entirely and return empty links (#2-fix).
        if status == "success" and _want_discovery_links:
            discovered_links, discovered_link_count, discovered_links_truncated = (
                _extract_discovered_links(html, response.final_url, max_links=limits.get("max_discovered_links", DEFAULT_MAX_DISCOVERED_LINKS), tree=tree)
            )
        else:
            discovered_links, discovered_link_count, discovered_links_truncated = [], 0, False
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            http_status=response.status_code,
            content_type=content_type,
            elapsed_ms=response.elapsed_ms,
            error=None if status == "success" else classification,
        )
        # T3/#2.1: release the large raw-HTML string before the result is built
        # when the caller did not request it (engine also strips at spill time).
        if not include_html:
            html = None
        return _make_engine_result(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=response.final_url,
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            selected_strategy=strategy if status == "success" else None,  # type: ignore[arg-type]
            attempt=attempt,
            http_status=response.status_code,
            headers=headers,
            content_type=content_type,
            html=html,
            text=text,
            markdown=markdown,
            extraction_method=extraction_method,
            quality_score=quality,
            classification_evidence=classification_evidence,
            diagnostics=diagnostics,
            discovered_link_count=discovered_link_count,
            discovered_links_truncated=discovered_links_truncated,
            discovered_links=discovered_links,
            error=None if status == "success" else classification,
            next_page_url=next_page_url,
            validators=origin_validators,
        )
    except Exception as error:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error_code = _classify_error_code(error)
        # Never expose raw exception text in the API response (it can leak DNS
        # names, internal filesystem paths, or stack detail). Surface only a
        # stable code to the caller; the raw detail is logged server-side for
        # debugging and is NOT returned in diagnostics.
        logger.warning(
            "http_strategy_result_error",
            extra={"error_code": error_code, "error_detail": str(error)},
        )
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status="failed",
            classification=error_code,
            http_status=None,
            content_type=None,
            elapsed_ms=elapsed_ms,
            error=error_code,
        )
        return _make_engine_result(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=None,
            status="failed",
            classification="network_error",
            attempt=attempt,
            http_status=None,
            diagnostics={
                "retry": retry_diagnostics,
                "runtime": {"elapsed_ms": elapsed_ms},
                "request_profile": policy.get("request_profile") or {},
            },
            error=error_code,
        )


# ---------------------------------------------------------------------------
# Document text annotation (agenda/legal document formatting)
# ---------------------------------------------------------------------------


