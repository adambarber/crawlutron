"""Owns request profile validation, cookie headers, fingerprints, and header assembly.

This module owns the policy-to-request identity seam and origin-scoped credentials.
It must not fetch, decode, extract, or construct crawl results.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from app.api_models import CrawlPolicy
from app.constants import DEFAULT_USER_AGENT, HTTP_CFFI_IMPERSONATE
from app.crawl.credentials import apply_credentials as _apply_credentials
from app.crawl.strategies import cookie_jar
from app.crawl.strategies.cookie_jar import _COOKIE_NAMESPACE


def cookie_namespace_for(profile: Mapping[str, Any]) -> str:
    """Return a per-request cookie namespace, or ``""`` for the shared default.

    Callers running multi-tenant crawls should set
    ``request_profile.cookie_namespace`` to a tenant id so cookies from
    different callers cannot leak into each other's sessions. Empty / unset
    preserves the legacy shared behavior so single-tenant callers and the
    test suite see no change.
    """
    value = profile.get("cookie_namespace")
    return str(value).strip() if isinstance(value, str) else ""


def _cookie_header_for_url(profile: Mapping[str, Any], url: str) -> str:
    """Build a Cookie header from policy cookies and the persistent jar."""
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
    jar_header = cookie_jar._GLOBAL_COOKIE_JAR.cookie_header_for(url, namespace=namespace)
    if jar_header:
        parts.append(jar_header)

    return "; ".join(parts)


@runtime_checkable
class _TransportHeaders(Protocol):
    """A transport's own header multidict, which keeps duplicate values.

    The two transports disagree about what a header map is -- httpx's is
    ``httpx.Headers``, curl_cffi's is a libcurl multidict that is nothing like
    it -- but both keep repeated header names separate, and that is the
    property the cookie jar needs. Normalising either into a ``dict`` first
    would comma-join duplicate ``Set-Cookie`` names into a single entry, which
    ``Headers.items()`` does and ``dict`` construction always does.

    So this is the shape the *transport* hands over, and it is deliberately not
    a ``Mapping``: a plain ``dict`` of headers is accepted where a caller
    offers one, but it is the one shape that cannot express a repeated header,
    and naming the difference is what keeps the cookie path honest.
    """

    def get(self, key: str, default: str = "") -> str: ...
    def get_list(self, key: str) -> list[str]: ...
    def items(self) -> Iterable[tuple[str, str]]: ...


def _store_set_cookies(
    url: str,
    response_headers: _TransportHeaders | dict[str, str],
    *,
    namespace: str | None = None,
) -> None:
    """Extract Set-Cookie headers from a response into the persistent jar.

    Uses ``get_list`` to capture all ``Set-Cookie`` headers (httpx 0.28+
    returns comma-joined values from ``.items()`` for duplicate keys).
    ``namespace`` isolates cookies for multi-tenant callers; when not
    provided, the per-request cookie namespace contextvar is used.

    The parameter names ``_TransportHeaders`` because the body branches on it:
    the ``isinstance`` below is the whole reason that protocol exists, and a
    signature that omitted it made that branch unreachable to any checker -- so
    the one caller shape the function was written to accept was the one it
    could not legally be handed.
    """
    if namespace is None:
        namespace = _COOKIE_NAMESPACE.get()
    # A transport multidict keeps repeated header names separate, which is the
    # only reason to read Set-Cookie through it; a plain dict has nothing to
    # keep separate, so it is read as-is. httpx.Headers arrives through the
    # first branch too -- it satisfies the protocol -- so there is no
    # httpx-specific case to keep in step with this one.
    if isinstance(response_headers, _TransportHeaders):
        set_cookies = response_headers.get_list("set-cookie")
    else:
        set_cookies = [v for k, v in response_headers.items() if k.lower() == "set-cookie"]
    for cookie in set_cookies:
        cookie_jar._GLOBAL_COOKIE_JAR.add_cookie_header(url, cookie, namespace=namespace)


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


def _rotating_index(profile: Mapping[str, Any], domain: str, pool_size: int) -> int:
    """Deterministic per-host pool index so repeated crawls of the same host keep a
    stable fingerprint (the disk content-cache key stays consistent) while different
    hosts rotate through the pool."""
    seed = f"{profile.get('fingerprint_seed', '')}:{domain}".encode()
    return int(hashlib.sha256(seed).hexdigest(), 16) % pool_size


def _select_fingerprint(profile: Mapping[str, Any], domain: str) -> dict[str, str]:
    """Select fingerprint signals for a host. Rotation is deterministic per host.
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


def _accept_encoding() -> str:
    """The encodings this process will actually decode.

    Every one of these is a declared dependency, so the list is a constant rather
    than a capability probe. It was conditional on ``_brotli_available`` because
    brotli was documented as optional, and a server that returned a brotli body
    this process then refused to decode failed the crawl on a request we had
    chosen to make -- which is the correct reason to stop declining ``br``, and
    not one to keep declining it in a deployment that cannot exist.

    Its own function rather than an inline constant: the builder that reads it is
    already long, and the list is a claim about the decoder
    set that a reader should be able to check against
    ``app.crawl.decompression._DECODABLE_ENCODINGS`` in one place.
    """
    return "gzip, deflate, br"


def _headers_for_strategy(
    strategy: str,
    policy: CrawlPolicy,
    url: str,
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    resolved_profile = profile if profile is not None else policy.request_profile
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": _accept_encoding(),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    user_agent_family = str(resolved_profile.get("user_agent_family", "generic_crawler"))
    is_browser_like = strategy == HTTP_CFFI_IMPERSONATE or user_agent_family == "desktop_chrome"
    # Fingerprint injection/rotation: deterministic per-host selection so a single
    # crawl rotates fingerprints across hosts while staying stable per host.
    fp = _select_fingerprint(resolved_profile, _domain_seed(url))
    if "user_agent" in fp:
        headers["User-Agent"] = fp["user_agent"]
        is_browser_like = is_browser_like or "Mozilla" in fp["user_agent"]
    elif is_browser_like:
        # Firefox identity: must match CFFI_IMPERSONATE_TARGET AND the Camoufox
        # browser strategy so cookie-bootstrap sessions carry a consistent
        # User-Agent across strategies.
        if strategy == HTTP_CFFI_IMPERSONATE:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"
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
    configured_user_agent = os.environ.get("CRAWLUTRON_USER_AGENT", "").strip()
    if configured_user_agent:
        headers["User-Agent"] = configured_user_agent
    cookie_header = _cookie_header_for_url(resolved_profile, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    headers.update(policy.headers)
    # Origin-scoped credentials last: the value is attached only when this
    # exact URL's origin holds one, and the redirect loop's credential scrub
    # strips it on any cross-host hop (same chokepoint as cookies).
    _apply_credentials(headers, url, policy.credentials)
    return headers
