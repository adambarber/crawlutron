"""HTTP fetch strategy: direct + curl_cffi impersonation, conditional caching."""

from __future__ import annotations

import asyncio
import inspect
import re
import socket
import ssl
import time
import threading
import tempfile
import hashlib
from urllib.parse import urlparse
import json
import logging
logger = logging.getLogger(__name__)
import base64
from dataclasses import asdict
from typing import Any

import httpx
import urllib.error

try:
    from charset_normalizer import from_bytes as _charset_from_bytes
except ImportError:
    _charset_from_bytes = None  # type: ignore[assignment]
import urllib.request

from app.classify import classify_page_with_evidence
from app.constants import (
    DEFAULT_EXTRACTION,
    EXTRACTION_DOCUMENT,
    EXTRACTION_FULL,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_DOM_TEXT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    SUCCESS_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
)
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache


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

from app.crawl.decompression import decompress_body as _decompress_body, DecompressionTooLargeError
from app.crawl.extraction import (
    _detect_pagination,
    _extract_discovered_links,
    _extract_document,
    _extract_html,
    _html_diagnostics,
    extract_js_endpoints,
    extract_content,
)
from app.crawl.result_helpers import _binary_no_text_result, _make_engine_result
from app.models import CrawlStrategyAttempt, HttpFetchResult, EngineCrawlResult, HttpFetcher
from app.validation import normalize_public_http_url, registrable_domain
from urllib.parse import urljoin, urlparse

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
_DEFAULT_HTTP_CLIENT: "httpx.AsyncClient | None" = None
_HTTP_CLIENT_INIT_LOCK = threading.Lock()  # serializes first-time client construction


def _default_settings() -> "CrawlerSettings":
    """Get default crawler settings for HTTP client configuration."""
    from app.settings import CrawlerSettings
    return CrawlerSettings.from_mapping()


def _build_async_client(settings: "CrawlerSettings | None" = None) -> "httpx.AsyncClient":
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
        headers={"User-Agent": "dev-stack-browser-crawler/0.2 (+https://local.dev-stack/crawler)"},
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
_PENDING_CLIENT_CLOSE: "list[httpx.AsyncClient]" = []


async def _drain_pending_client_closes() -> None:
    """Close any superseded pooled clients on the current (live) loop.

    ``_schedule_client_close`` fires on the *dying* loop, where the scheduled
    task is cancelled before ``aclose`` completes (every ``asyncio.run`` exit).
    Draining here, inside a fetch that is already running on a live loop,
    actually releases those sockets instead of leaking them.
    """
    while _PENDING_CLIENT_CLOSE:
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


def get_async_http_client(settings: "CrawlerSettings | None" = None) -> "httpx.AsyncClient":
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
            if old_client not in _PENDING_CLIENT_CLOSE:
                _PENDING_CLIENT_CLOSE.append(old_client)
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
    """

    def __init__(self) -> None:
        self._cookies: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def add_cookie_header(self, url: str, set_cookie_header: str) -> None:
        """Parse a ``Set-Cookie`` header and store the cookie for the URL's domain."""
        from http.cookies import SimpleCookie

        host = (urlparse(url).hostname or "").lower()
        jar = SimpleCookie()
        try:
            jar.load(set_cookie_header)
        except Exception:
            return
        with self._lock:
            target = self._cookies.setdefault(host, {})
            for cookie in jar.values():
                target[cookie.key] = cookie.value

    def cookie_header_for(self, url: str) -> str:
        """Return a ``Cookie`` header string for the given URL's domain."""
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            domain_cookies = dict(self._cookies.get(host, {}))
        if not domain_cookies:
            return ""
        return "; ".join(f"{key}={value}" for key, value in domain_cookies.items())

    def clear(self) -> None:
        with self._lock:
            self._cookies.clear()

    def save_to_file(self, path: str) -> None:
        """Persist the cookie jar to a JSON file atomically.

        Writes to a temp file in the same directory and ``os.replace``s it into
        place, so a crash (or write error) mid-serialization cannot leave a
        truncated/corrupted file that silently loses all persisted cookies.
        """
        import json
        import os

        with self._lock:
            data = {domain: dict(cookies) for domain, cookies in self._cookies.items()}
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
    def load_from_file(cls, path: str) -> "CookieJar":
        """Load a cookie jar from a JSON file. Returns empty jar if file missing/invalid."""
        import json, os

        jar = cls()
        if not os.path.exists(path):
            return jar
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                with jar._lock:
                    for domain, cookies in data.items():
                        if isinstance(cookies, dict):
                            jar._cookies[domain] = cookies
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


def _check_ssrf_protection(url: str) -> None:
    """Resolve the URL hostname and abort if any IP is private/loopback.

    Set BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1 to disable for local testing.
    """
    import os
    if os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS") == "1":
        return
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # An unresolvable hostname is not a valid public target; fail closed
        # rather than silently allowing it. Previously this let unresolvable
        # hostnames bypass the private-IP block and reach loopback via the pin
        # fallback in _resolve_and_pin_host.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}")
    for entry in addrinfo:
        ip = entry[4][0]
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
    if os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS") == "1":
        # Local-dev escape hatch: bypass resolution but connect to the REAL
        # hostname. Previously this hardcoded 127.0.0.1, which misrouted every
        # fetch/webhook to loopback and broke virtual-host routing.
        return hostname, hostname
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Do NOT fall back to loopback. Returning the original hostname makes
        # the HTTP client attempt resolution itself and fail with a connection
        # error instead of silently connecting to 127.0.0.1 (SSRF bypass, F9).
        return hostname, hostname
    for entry in addrinfo:
        ip = entry[4][0]
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
    """Resolve + pin the target IP (DNS-rebinding safe) and build the request.

    Returns ``(pinned_url, request_headers)`` where ``pinned_url`` connects to
    the single resolved IP and ``request_headers`` preserves the original Host
    header so virtual-host routing still works. Shared by both fetchers.
    """
    _check_ssrf_protection(url)
    hostname, pinned_ip = _resolve_and_pin_host(url)
    parsed = urlparse(url)
    pinned_url = (
        f"{parsed.scheme}://{pinned_ip}:{parsed.port or (80 if parsed.scheme == 'http' else 443)}"
        f"{parsed.path or '/'}"
        + (f"?{parsed.query}" if parsed.query else "")
    )
    request_headers = dict(headers)
    if parsed.hostname:
        request_headers["Host"] = parsed.hostname
    return pinned_url, request_headers


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


async def _default_http_fetcher(url: str, headers: dict[str, str], timeout_seconds: int, max_content_length: int = 50 * 1024 * 1024) -> HttpFetchResult:
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
        pinned_url, hop_headers = _build_pinned_request(current_url, request_headers)
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
                    body = _decompress_body(bytes(raw), encoding, max_output_bytes=max_content_length)
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


async def _default_cffi_fetcher(url: str, headers: dict[str, str], timeout_seconds: int, max_content_length: int = 50 * 1024 * 1024) -> HttpFetchResult:
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
        async with cffi_requests.AsyncClient(impersonate="chrome124", timeout=float(timeout_seconds)) as client:
            current_url = url
            hops = 0
            # Work with a copy of headers so we can mutate per-hop
            request_headers = dict(headers)
            while True:
                pinned_url, hop_headers = _build_pinned_request(current_url, request_headers)
                try:
                    response = await client.get(pinned_url, headers=hop_headers, allow_redirects=False)
                except Exception:
                    # Fall back to plain httpx for this request on transport errors.
                    return await _default_http_fetcher(url, headers, timeout_seconds, max_content_length)
                # Content-length check (#5)
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_content_length:
                    return HttpFetchResult(
                        url=url, final_url=str(response.url), status_code=413,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=b"", elapsed_ms=int((time.monotonic() - 0) * 1000),
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
                # Decompress with a size cap (decompression-bomb guard).
                encoding = response.headers.get("content-encoding", "")
                try:
                    body = _decompress_body(response.content, encoding, max_output_bytes=max_content_length)
                except DecompressionTooLargeError:
                    return HttpFetchResult(
                        url=url, final_url=str(response.url), status_code=413,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=b"", elapsed_ms=elapsed_ms,
                    )
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
                )
    except Exception:  # any cffi failure falls back to plain httpx
        return await _default_http_fetcher(url, headers, timeout_seconds, max_content_length)


def _cookie_header_for_url(profile: dict[str, Any], url: str) -> str:
    """Build a Cookie header from both policy cookies and the global persistent jar (#2)."""
    host = (urlparse(url).hostname or "").lower()
    parts: list[str] = []

    # Static policy cookies
    if profile.get("cookie_jar") in {"per_host", "session"}:
        cookies = profile.get("cookies") or {}
        if isinstance(cookies, dict):
            host_cookies = cookies.get(host) or {}
            if isinstance(host_cookies, dict):
                parts.extend(f"{key}={value}" for key, value in host_cookies.items())

    # Persistent cookies from prior responses
    jar_header = _GLOBAL_COOKIE_JAR.cookie_header_for(url)
    if jar_header:
        parts.append(jar_header)

    return "; ".join(parts)


def _store_set_cookies(url: str, response_headers: "httpx.Headers | dict[str, str]") -> None:
    """Extract Set-Cookie headers from a response into the global jar (#2).

    Uses ``get_list`` to capture all ``Set-Cookie`` headers (httpx 0.28+
    returns comma-joined values from ``.items()`` for duplicate keys).
    """
    # Accept both httpx.Headers (for get_list) and plain dict (for tests)
    if hasattr(response_headers, "get_list"):
        set_cookies = response_headers.get_list("set-cookie")
    else:
        # Fallback for plain dict (test mocks) - iterate items
        set_cookies = [v for k, v in response_headers.items() if k.lower() == "set-cookie"]
    for cookie in set_cookies:
        _GLOBAL_COOKIE_JAR.add_cookie_header(url, cookie)


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
    seed = f"{profile.get('fingerprint_seed', '')}:{domain}".encode("utf-8")
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
        "User-Agent": "dev-stack-browser-crawler/0.2 (+https://local.dev-stack/crawler)",
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


def _decode_body(body: bytes, content_type: str | None, headers: dict[str, str] | None = None) -> str:
    """Decode response body, using charset-normalizer to detect encoding when
    the declared charset produces replacement characters or is missing (#3)."""
    if headers:
        body = _decompress_body(body, headers.get("content-encoding", ""))
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip()
    try:
        text = body.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace")
    # If the declared charset produced replacement chars, sniff with charset-normalizer
    if "\ufffd" in text and _charset_from_bytes is not None:
        detected = _charset_from_bytes(body)
        best = detected.best()
        if best is not None and best.encoding:
            text = str(best)
    return text


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


def _content_metrics(text: str | None, html: str | None) -> dict[str, int | float | bool]:
    visible_text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    link_count = len(re.findall(r"<a\b", html or "", flags=re.IGNORECASE))
    frame_count = len(re.findall(r"<iframe\b|<frame\b", html or "", flags=re.IGNORECASE))
    form_count = len(re.findall(r"<form\b", html or "", flags=re.IGNORECASE))
    return {
        "visible_text_length": visible_text_length,
        "html_length": html_length,
        "link_count": link_count,
        "frame_count": frame_count,
        "form_count": form_count,
        "has_frameset": bool(re.search(r"<frameset\b", html or "", flags=re.IGNORECASE)),
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
    max_content_length: int = 50 * 1024 * 1024,
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
) -> dict[str, Any]:
    discovery = policy.get("discovery") or {}
    limits = policy.get("limits", {})
    diagnostics: dict[str, Any] = {}
    parsed = urlparse(normalized_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_sitemaps: list[str] = []
    if discovery.get("robots"):
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
        for sitemap_url in (urljoin(origin, entry) for entry in candidate_sitemaps):
            try:
                # Sitemap entries are attacker-influenced and may point at a
                # different origin: never send credential headers there (H-2).
                safe_headers = {
                    key: value for key, value in headers.items()
                    if key.lower() not in _CREDENTIAL_HEADERS
                    or urlparse(sitemap_url).hostname == urlparse(normalized_url).hostname
                }
                sitemap_response = http_fetcher(sitemap_url, safe_headers, limits.get("timeout_seconds", 10))
                if inspect.iscoroutine(sitemap_response):
                    sitemap_response = await sitemap_response
                sitemap_headers = {key.lower(): value for key, value in sitemap_response.headers.items()}
                try:
                    sitemap_body = _decompress_body(
                        sitemap_response.body,
                        sitemap_headers.get("content-encoding", ""),
                        max_output_bytes=8 * 1024 * 1024,
                    )
                except DecompressionTooLargeError:
                    continue
                sitemap_text = _decode_body(sitemap_body, sitemap_headers.get("content-type"))
                sitemap_urls.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_text, flags=re.IGNORECASE))
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
) -> EngineCrawlResult:
    started = time.monotonic()
    retry_diagnostics: dict[str, Any] = {"attempts": 0, "transient_errors": []}
    # Caller-supplied conditional validators (freshness monitoring): sent as
    # If-None-Match / If-Modified-Since so an unchanged page returns 304.
    caller_conditional: dict[str, str] = {}
    if caller_validators:
        if caller_validators.get("if_none_match"):
            caller_conditional["If-None-Match"] = caller_validators["if_none_match"]
        if caller_validators.get("if_modified_since"):
            caller_conditional["If-Modified-Since"] = caller_validators["if_modified_since"]
    # Provide default limits for backward compatibility with tests
    limits = policy.get("limits", {})
    timeout_seconds = limits.get("timeout_seconds", 10)
    max_retries = limits.get("max_retries", 0)
    max_content_length = limits.get("max_content_length_bytes", 50 * 1024 * 1024)
    base_headers = _headers_for_strategy(strategy, policy, normalized_url)
    request_headers = dict(base_headers)
    if cache is not None:
        request_headers.update(cache.conditional_headers(normalized_url))
    if caller_conditional:
        request_headers.update(caller_conditional)

    extraction = policy.get("extraction", DEFAULT_EXTRACTION)

    # Disk content cache (#5): serve an unchanged URL from local disk instead of
    # re-downloading. The key is derived from the *base* request headers (excluding
    # HTTP conditional-revalidation headers) so it stays stable across crawls -- the
    # conditional headers only change the 304 handshake, never the payload.
    ckey: str | None = None
    content_hit = False
    revalidated_304 = False
    if content_cache is not None:
        ckey = content_cache.make_key(
            "http", normalized_url, strategy,
            json.dumps(sorted(base_headers.items()), separators=(",", ":")),
            extraction, policy.get("css_selector"), policy.get("xpath"),
        )
        cached = content_cache.get(ckey)
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
                    content_cache.set(ckey, _serialize_response(response))
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
        decompressed = _decompress_body(response.body, headers.get("content-encoding", ""))
        body_text = _decode_body(decompressed, content_type)
        is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(("<html", "<!doctype html"))
        html = body_text if is_html else None
        if extraction == EXTRACTION_DOCUMENT:
            extracted = _extract_document(decompressed, response.final_url)
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
            extracted = extract_content(decompressed, content_type, response.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
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
            "request_profile": {
                "user_agent_family": str(policy.get("request_profile", {}).get("user_agent_family", "generic_crawler")),
                "cookie_jar": str(policy.get("request_profile", {}).get("cookie_jar", "none")),
            },
            "retry": retry_diagnostics,
            "runtime": {"elapsed_ms": response.elapsed_ms, "final_url": response.final_url},
            "content_metrics": _content_metrics(text, html),
        }
        diagnostics.update(_html_diagnostics(html, response.final_url))
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            diagnostics["cooldown"] = {"retry_after_seconds": retry_after, "scope": "domain"}
        diagnostics.update(
            await _discovery_diagnostics(
                normalized_url=normalized_url,
                html=html,
                policy=policy,
                headers=request_headers,
                http_fetcher=http_fetcher,
            )
        )
        # Emitted discovery URLs are attacker-influenced: only http(s) targets
        # may leave the service (no javascript:, file:, link-local oddities).
        raw_endpoints = extract_js_endpoints(html, normalized_url)
        diagnostics["js_endpoints"] = [
            endpoint
            for endpoint in raw_endpoints
            if urlparse(str(endpoint.get("url", ""))).scheme in ("", "http", "https")
        ]
        next_page_url = _detect_pagination(html, response.final_url)
        if next_page_url and urlparse(next_page_url).scheme not in ("http", "https"):
            next_page_url = None
        if next_page_url:
            diagnostics["next_page_url"] = next_page_url
        quality = _quality_score(status, classification, text, html, response.status_code)
        # Surface origin validators so callers can revalidate cheaply later.
        origin_validators = {
            key: value
            for key, value in (
                ("etag", headers.get("etag")),
                ("last_modified", headers.get("last-modified")),
            )
            if value
        }
        if status == "success" and diagnostics["content_metrics"]["visible_text_length"] >= 40:
            quality = max(quality, 0.95)
        discovered_links, discovered_link_count, discovered_links_truncated = (
            _extract_discovered_links(html, response.final_url, max_links=limits.get("max_discovered_links", 250)) if status == "success" else ([], 0, False)
        )
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            http_status=response.status_code,
            content_type=content_type,
            elapsed_ms=response.elapsed_ms,
            error=None if status == "success" else classification,
        )
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


def _format_extracted_document_text(text: str, *, source_url: str) -> str:
    """Normalize scraper-extracted document text for downstream annotation."""
    lines = [_clean_document_line(line) for line in text.splitlines()]
    records: list[str] = []
    action_by_item: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        if _is_document_non_item_section(line):
            index += 1
            continue

        range_action_match = re.match(r"^(?P<start>\d+[a-z])\s*-\s*(?P<end>\d+[a-z])\s+(?P<action>.+)$", line, flags=re.IGNORECASE)
        if range_action_match:
            action = range_action_match.group("action").strip()
            for item_number in _document_item_range(range_action_match.group("start"), range_action_match.group("end")):
                action_by_item[item_number] = action
            index += 1
            continue

        action_match = re.match(r"^(?P<number>\d+[a-z])\s+(?P<action>.+)$", line, flags=re.IGNORECASE)
        if action_match and not line.endswith("."):
            action_by_item[action_match.group("number").lower()] = action_match.group("action").strip()
            index += 1
            continue

        section_match = re.match(r"^(?P<number>\d+)\.$", line)
        if section_match:
            title_index = _next_non_empty_document_line(lines, index + 1)
            if title_index is not None and not _is_document_item_marker(lines[title_index]):
                records.append(f"{section_match.group('number')}. {lines[title_index]}")
                index = title_index + 1
                continue

        item_match = re.match(r"^(?P<number>\d+[a-z])\.$", line, flags=re.IGNORECASE)
        if item_match:
            number = item_match.group("number").lower()
            title_lines, next_index = _collect_document_item_lines(lines, index + 1)
            if title_lines:
                action = action_by_item.get(number)
                action_suffix = f" [Action: {action}]" if action else ""
                records.append(f"{number}. {' '.join(title_lines)}{action_suffix}")
                index = next_index
                continue

        records.append(line)
        index += 1

    return "\n\n".join(_merge_document_paragraph_records(records)).strip()


def _merge_document_paragraph_records(records: list[str]) -> list[str]:
    merged: list[str] = []
    for record in records:
        if merged and _should_join_document_paragraph(merged[-1], record):
            merged[-1] = _join_document_paragraph_lines(merged[-1], record)
            continue
        merged.append(record)
    return merged


def _should_join_document_paragraph(previous: str, current: str) -> bool:
    if _is_numbered_document_record(previous) or _is_numbered_document_record(current):
        return False
    if _looks_like_document_heading(previous) or _looks_like_document_heading(current):
        return False
    if re.match(r"^[,;:.!?)]", current):
        return True
    if not re.search(r"[.!?:;)]$", previous) and _looks_like_document_prose(previous) and _looks_like_document_prose(current):
        return True
    return _looks_like_document_prose(previous) and _looks_like_document_prose(current) and current[:1].islower()


def _join_document_paragraph_lines(previous: str, current: str) -> str:
    if re.match(r"^[,;:.!?)]", current):
        return f"{previous}{current[0]} {current[1:].lstrip()}"
    return f"{previous} {current}"


def _looks_like_document_prose(record: str) -> bool:
    return bool(re.search(r"[a-z]", record)) and len(record.split()) >= 3


def _looks_like_document_heading(record: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", record)
    return bool(letters) and letters.upper() == letters and len(record.split()) <= 8


def _is_numbered_document_record(record: str) -> bool:
    return bool(re.match(r"^\d+[a-z]?\.\s", record, flags=re.IGNORECASE))


def _clean_document_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\f", " ")).strip()


def _next_non_empty_document_line(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        if lines[index]:
            return index
    return None


def _is_document_item_marker(line: str) -> bool:
    return bool(re.match(r"^\d+[a-z]\.?$", line, flags=re.IGNORECASE))


def _is_document_record_boundary(line: str) -> bool:
    return bool(
        re.match(r"^\d+\.$", line)
        or re.match(r"^\d+[a-z]\.?$", line, flags=re.IGNORECASE)
        or re.match(r"^\d+[a-z]\s+.+$", line, flags=re.IGNORECASE)
        or _is_document_non_item_section(line)
        or line.lower() == "attachments"
    )


def _is_document_non_item_section(line: str) -> bool:
    return line.upper().startswith("PUBLIC PARTICIPATION")


def _collect_document_item_lines(lines: list[str], start: int) -> tuple[list[str], int]:
    title_lines: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        numbered_continuation = re.match(r"^\d+\.\s+(?P<text>.+)$", line)
        if numbered_continuation:
            title_lines.append(numbered_continuation.group("text"))
            index += 1
            continue
        if re.match(r"^\d+\.$", line):
            next_index = _next_non_empty_document_line(lines, index + 1)
            if next_index is not None and not _is_document_non_item_section(lines[next_index]):
                title_lines.append(lines[next_index])
                index = next_index + 1
                continue
            break
        if _is_document_record_boundary(line):
            break
        title_lines.append(line)
        index += 1
    return title_lines, index


def _document_item_range(start: str, end: str) -> list[str]:
    start_match = re.match(r"^(\d+)([a-z])$", start.lower())
    end_match = re.match(r"^(\d+)([a-z])$", end.lower())
    if not start_match or not end_match or start_match.group(1) != end_match.group(1):
        return [start.lower(), end.lower()]
    prefix = start_match.group(1)
    return [f"{prefix}{chr(code)}" for code in range(ord(start_match.group(2)), ord(end_match.group(2)) + 1)]
