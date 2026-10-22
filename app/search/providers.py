"""Search providers for ``POST /v1/search``; which index backs it is a config
decision, not a code one. The provider families live beside this module; this
one owns the shared request/collect contract and the registry.
"""

from __future__ import annotations

import abc
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.safety.bounded_io import BodyTooLarge, read_capped_async
from app.safety.validation import HostPolicy
from app.transport.http_client_pool import get_async_http_client
from app.transport.pinned_transport import (
    ResolverBudgetExceeded,
    bounded_thread_resolve,
    build_pinned_request,
    sni_extension,
)
from app.transport.timeouts import Timeouts

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_SERPER_URL = "https://google.serper.dev/search"
_DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
_DUCKDUCKGO_REDIRECT = "//duckduckgo.com/l/"

#: The whole provider endpoint registry: every URL a provider fetch may target.
#: Read by the #386 trust-boundary test (no caller input reaches this set).
PROVIDER_ENDPOINTS = frozenset((_TAVILY_URL, _BRAVE_URL, _SERPER_URL, _DUCKDUCKGO_URL))

# Result text from a search engine is untrusted; truncate titles/snippets so a
# hostile provider cannot push unbounded strings into responses.
MAX_TEXT_LENGTH = 2000


@dataclass(frozen=True)
class SearchResult:
    """A normalized, SSRF-screened search result."""

    url: str
    title: str
    description: str = ""


class SearchProviderError(Exception):
    """A provider call failed (network, non-2xx, or an unparsable body)."""


# Search responses are snippets: a 1 MiB ceiling is generous (a DDG HTML
# page is ~200 KB) and keeps hostile provider bodies out of RAM and lxml.
_SEARCH_RESPONSE_MAX_BYTES = 1 * 1024 * 1024


class SearchProviderUnavailable(Exception):
    """A provider was selected but its credentials are not configured."""


class BaseProvider(abc.ABC):
    """What every search provider shares: a name, the policy, and the shape.

    Each family supplies its own fetch and parse, so both hooks are abstract:
    an incomplete provider fails at construction, not at the first search.
    """

    name: str

    def __init__(self, host_policy: HostPolicy) -> None:
        self._host_policy = host_policy

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        """Query this provider and return SSRF-screened, normalized results."""
        status, body = await self._fetch(query, limit, timeout_seconds)
        return self._results(status, body, limit)

    @abc.abstractmethod
    async def _fetch(self, query: str, limit: int, timeout_seconds: float) -> tuple[int, str]:
        """This provider's HTTP exchange: the status and the body it returned."""

    @abc.abstractmethod
    def _results(self, status: int, body: str, limit: int) -> list[SearchResult]:
        """This provider's response shape, read into normalized results."""


async def provider_http(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float,
) -> tuple[int, str]:
    """Issue a provider request over the shared pooled HTTP client.

    Module-level so tests can patch ``provider_http`` to inject fixture responses.
    """
    client = get_async_http_client()
    timeout = Timeouts.for_request(timeout_seconds).as_httpx()
    request_url = httpx.URL(url)
    if params:
        request_url = request_url.copy_merge_params(params)
    pinned_url, hop_headers, extensions = await _pinned_provider_request(
        request_url, headers, timeout_seconds
    )
    try:
        async with client.stream(
            method,
            pinned_url,
            json=json_body,
            data=data,
            headers=hop_headers,
            timeout=timeout,
            extensions=extensions,
        ) as response:
            try:
                raw = await read_capped_async(response.aiter_bytes(), _SEARCH_RESPONSE_MAX_BYTES)
            except BodyTooLarge as error:
                raise SearchProviderError(
                    f"provider body exceeded {_SEARCH_RESPONSE_MAX_BYTES} bytes"
                ) from error
            text = raw.decode(response.encoding or "utf-8", errors="replace")
            return response.status_code, text
    except httpx.TransportError as error:
        # A refused connection, DNS failure, or read timeout is a provider
        # fault, not a transport hole: shape it into the error envelope.
        raise SearchProviderError(f"provider request failed: {error}") from error


async def _pinned_provider_request(
    request_url: httpx.URL,
    headers: dict[str, str] | None,
    timeout_seconds: float,
) -> tuple[str, dict[str, str], dict[str, str] | None]:
    """The pinned provider request (#386): the merged URL admitted, the connect
    pinned to the freshly validated IP, SNI on the hostname. Fixed endpoint
    registry, no caller input in the URL (pinned by test)."""
    admitted = NormalizedUrl(str(request_url), policy=HostPolicy.unrestricted())
    hop_headers = dict(headers or {})

    def pin() -> tuple[str, dict[str, str]]:
        return build_pinned_request(admitted, hop_headers)

    try:
        connect_url, hop_headers = await bounded_thread_resolve(pin, timeout_seconds=timeout_seconds)
    except (ValueError, ResolverBudgetExceeded):
        raise SearchProviderError("provider endpoint could not be resolved to a public address") from None
    return connect_url, hop_headers, sni_extension(admitted.scheme, admitted.host)


def clip_text(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT_LENGTH else text[:MAX_TEXT_LENGTH].rstrip() + "…"


def normalize_result_url(raw: str, *, policy: HostPolicy) -> str | None:
    """Return ``raw`` when it is a crawlable public HTTP(S) URL, else ``None``.
    Invalid targets are dropped, never rewritten, to keep the response honest:
    a loopback/private result is not a URL a caller can legally crawl.
    """
    normalized = admit_or_none(raw, policy=policy)
    return str(normalized) if normalized is not None else None


def collect_results(
    items: object,
    *,
    url_key: str,
    title_key: str,
    description_key: str,
    policy: HostPolicy,
) -> list[SearchResult]:
    """Normalize a keyed provider's raw result list into ``SearchResult``s.
    The three keyed providers differ only in which key holds which field, so the
    validation/dedupe/clip flow is one contract; non-dicts and non-public URLs are dropped.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = normalize_result_url(str(item.get(url_key) or ""), policy=policy)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            SearchResult(
                url=url,
                title=clip_text(str(item.get(title_key) or "")),
                description=clip_text(str(item.get(description_key) or "")),
            )
        )
    return results


def parse_provider_json(body: str) -> dict[str, JsonValue]:
    """An external provider body parsed as the JSON object it must be: keys the
    reader checks, values ``JsonValue`` -- a non-object body is an error at the door."""
    try:
        payload: JsonValue = json.loads(body)
    except (json.JSONDecodeError, TypeError) as error:
        raise SearchProviderError("provider returned non-JSON body") from error
    if not isinstance(payload, dict):
        raise SearchProviderError("provider returned a non-object JSON body")
    return payload


def provider_message(provider: str, status: int, payload: dict[str, JsonValue]) -> str:
    detail = payload.get("detail") or payload.get("error") or payload.get("message")
    if isinstance(detail, dict):
        detail = detail.get("message")
    return f"{provider} HTTP {status}" + (f": {detail}" if detail else "")


@dataclass(frozen=True)
class ProviderCall:
    """One provider request, minus the credential: method, endpoint and payload
    are the only per-provider facts about going over the wire."""

    method: str
    url: str
    params: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


def configured_search_providers(settings: Any) -> list[str]:
    """Names of providers that have the credentials they need, in priority order.

    ``auto`` falls back to the keyless DuckDuckGo provider when this is empty.
    """
    names: list[str] = []
    for name, key in (
        ("tavily", settings.tavily_api_key),
        ("brave", settings.brave_api_key),
        ("serper", settings.serper_api_key),
    ):
        if key:
            names.append(name)
    return names


def active_search_provider_name(settings: Any) -> str:
    """The provider ``auto`` selection would pick for these settings."""
    providers = configured_search_providers(settings)
    return providers[0] if providers else "duckduckgo"


def get_search_provider(settings: Any) -> BaseProvider:
    """Resolve the configured search provider; raise :class:`SearchProviderUnavailable`
    when an explicit provider was selected but its API key is missing."""
    # Imported here, not at module top, to break the provider-family cycle: the
    # api/html modules import the shared contract from this module.
    from app.search.providers_api import BraveProvider, SerperProvider, TavilyProvider
    from app.search.providers_html import DuckDuckGoProvider

    requested = (settings.search_provider or "auto").strip().lower()
    host_policy = HostPolicy.from_settings(settings)
    available: dict[str, BaseProvider] = {
        provider.name: provider
        for provider in (
            TavilyProvider(settings.tavily_api_key, host_policy) if settings.tavily_api_key else None,
            BraveProvider(settings.brave_api_key, host_policy) if settings.brave_api_key else None,
            SerperProvider(settings.serper_api_key, host_policy) if settings.serper_api_key else None,
        )
        if provider is not None
    }
    if requested == "auto":
        return next(iter(available.values())) if available else DuckDuckGoProvider(host_policy)
    if requested == "duckduckgo":
        return DuckDuckGoProvider(host_policy)
    provider = available.get(requested)
    if provider is not None:
        return provider
    raise SearchProviderUnavailable(
        f"search provider '{requested}' is not configured; "
        f"set CRAWLUTRON_{requested.upper()}_API_KEY (or CRAWLUTRON_SEARCH_PROVIDER=duckduckgo)"
    )


__all__ = [
    "PROVIDER_ENDPOINTS",
    "BaseProvider",
    "ProviderCall",
    "SearchProviderError",
    "SearchProviderUnavailable",
    "SearchResult",
    "active_search_provider_name",
    "clip_text",
    "collect_results",
    "configured_search_providers",
    "get_search_provider",
    "normalize_result_url",
    "parse_provider_json",
    "provider_message",
]
