"""Owns request profile validation, cookie headers, fingerprints, and header assembly.

This module owns the policy-to-request identity seam and origin-scoped credentials.
It must not fetch, decode, extract, or construct crawl results.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from app.admission.credentials import apply_credentials as apply_credentials
from app.models.crawl_policy import CrawlPolicy
from app.models.normalized_url import NormalizedUrl
from app.transport import cookie_jar
from app.transport.cookie_jar import COOKIE_NAMESPACE
from app.vocabulary.identity import DEFAULT_USER_AGENT
from app.vocabulary.strategies import HTTP_CFFI_IMPERSONATE


def cookie_namespace_for(profile: Mapping[str, Any]) -> str:
    """Return a per-request cookie namespace, or ``""`` for the shared default,
    so multi-tenant callers (who set ``cookie_namespace`` to a tenant id) never
    leak cookies across callers; empty or unset keeps the legacy shared behavior.
    """
    value = profile.get("cookie_namespace")
    return str(value).strip() if isinstance(value, str) else ""


def cookie_header_for_url(profile: Mapping[str, Any], url: NormalizedUrl) -> str:
    """Build a Cookie header from policy cookies and the persistent jar."""
    host = url.host
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
    namespace = cookie_namespace_for(profile) or COOKIE_NAMESPACE.get()
    jar_header = cookie_jar.global_cookie_jar().cookie_header_for(url, namespace=namespace)
    if jar_header:
        parts.append(jar_header)

    return "; ".join(parts)


@runtime_checkable
class TransportHeaders(Protocol):
    """A transport's own header multidict, keeping duplicate values apart -- the
    property the cookie jar needs, which a ``dict`` would comma-join. httpx and
    curl_cffi both keep repeated names, so this is deliberately not a ``Mapping``.
    """

    def get(self, key: str, default: str = "") -> str: ...
    def get_list(self, key: str) -> list[str]: ...
    def items(self) -> Iterable[tuple[str, str]]: ...


async def store_set_cookies(
    url: NormalizedUrl,
    response_headers: TransportHeaders | dict[str, str],
    *,
    namespace: str | None = None,
) -> None:
    """Extract all Set-Cookie headers into the persistent jar (via ``get_list``,
    since a dict comma-joins duplicates); ``namespace`` isolates multi-tenant
    cookies. Awaited from async frames: the jar twin keeps SQLite off the loop.
    """
    if namespace is None:
        namespace = COOKIE_NAMESPACE.get()
        # only reason to read Set-Cookie through it; a plain dict is read as-is, and
        # httpx.Headers satisfies the first branch too, so there is no httpx-specific case.
    if isinstance(response_headers, TransportHeaders):
        set_cookies = response_headers.get_list("set-cookie")
    else:
        set_cookies = [v for k, v in response_headers.items() if k.lower() == "set-cookie"]
    jar = cookie_jar.global_cookie_jar()
    for cookie in set_cookies:
        await jar.aadd_cookie_header(url, cookie, namespace=namespace)


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


def _rotating_index(profile: Mapping[str, Any], domain: str, pool_size: int) -> int:
    """Deterministic per-host pool index so repeated crawls of the same host keep a
    stable fingerprint (the disk content-cache key stays consistent) while different
    hosts rotate through the pool."""
    seed = f"{profile.get('fingerprint_seed', '')}:{domain}".encode()
    return int(hashlib.sha256(seed).hexdigest(), 16) % pool_size


def select_fingerprint(profile: Mapping[str, Any], url: NormalizedUrl) -> dict[str, str]:
    """Select fingerprint signals for a host; rotation is deterministic per host.
    Explicit pools are preferred, falling back to built-ins; anchoring on the
    host keeps them complementary to TLS impersonation, never conflicting.
    """
    domain = url.host
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
    """The encodings this process will actually decode -- a constant, since every
    one is a declared dependency. It once declined ``br`` on a probe, but a server
    returning a body this process refuses to decode fails a chosen crawl.
    """
    return "gzip, deflate, br"


def headers_for_strategy(
    strategy: str,
    policy: CrawlPolicy,
    url: NormalizedUrl,
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
    fp = select_fingerprint(resolved_profile, url)
    if "user_agent" in fp:
        headers["User-Agent"] = fp["user_agent"]
        is_browser_like = is_browser_like or "Mozilla" in fp["user_agent"]
    elif is_browser_like:
        # Firefox identity: must match CFFI_IMPERSONATE_TARGET AND the Camoufox browser
        # strategy, so cookie-bootstrap sessions carry a consistent User-Agent across both.
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
    cookie_header = cookie_header_for_url(resolved_profile, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    headers.update(policy.headers)
    # Origin-scoped credentials last: attached only when this exact URL's origin holds one,
    # and the redirect loop's scrub strips it on any cross-host hop (as with cookies).
    apply_credentials(headers, url, policy.credentials)
    return headers
