"""Search providers for ``POST /v1/search``.

Which search index backs the endpoint is a *configuration* decision, not a
code decision. The service ships three keyed providers (Tavily, Brave,
Serper) and one keyless best-effort provider (DuckDuckGo's HTML endpoint,
which datacenter IPs may captcha-block). ``auto`` selection activates the
first provider whose credentials are configured; an explicitly selected
provider whose key is missing raises :class:`SearchProviderUnavailable` so
the endpoint can surface an actionable 503 instead of a silent failure.

Every result URL is run through :func:`app.validation.normalize_public_http_url`
before it is returned: callers are expected to pipe search results straight
into ``/v1/crawl``, so the same SSRF hygiene that guards crawling applies
here. Loopback/private targets are dropped, never advertised.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from lxml import html as lxml_html

from app.bounded_io import BodyTooLarge, read_capped_async
from app.crawl.strategies.http_client_pool import get_async_http_client
from app.timeouts import Timeouts
from app.validation import HostPolicy, safe_public_url_or_none

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_SERPER_URL = "https://google.serper.dev/search"
_DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
_DUCKDUCKGO_REDIRECT = "//duckduckgo.com/l/"

# Result text from a search engine is untrusted; truncate titles/snippets so a
# hostile provider cannot push unbounded strings into responses.
_MAX_TEXT_LENGTH = 2000

_DUCKDUCKGO_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


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


class SearchProvider(Protocol):
    """A provider that turns a query into normalized ``SearchResult``s."""

    name: str

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]: ...


async def _provider_http(
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

    Module-level so tests can patch ``app.search.providers._provider_http``
    to inject fixture responses without touching the providers themselves.
    """
    client = get_async_http_client()
    timeout = Timeouts.for_request(timeout_seconds).as_httpx()
    async with client.stream(
        method,
        url,
        params=params,
        json=json_body,
        data=data,
        headers=headers,
        timeout=timeout,
    ) as response:
        try:
            raw = await read_capped_async(response.aiter_bytes(), _SEARCH_RESPONSE_MAX_BYTES)
        except BodyTooLarge as error:
            raise SearchProviderError(f"provider body exceeded {_SEARCH_RESPONSE_MAX_BYTES} bytes") from error
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        return response.status_code, text


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_TEXT_LENGTH else text[:_MAX_TEXT_LENGTH].rstrip() + "…"


def _normalize_url(raw: str, *, policy: HostPolicy) -> str | None:
    """Return ``raw`` when it is a crawlable public HTTP(S) URL, else ``None``.

    Dropping (rather than rewriting) invalid targets keeps the response honest:
    a result that points at loopback or a private network is not a public URL
    a caller can legally crawl, so it is not advertised.
    """
    normalized = safe_public_url_or_none(raw, policy=policy)
    return str(normalized) if normalized is not None else None


def _collect_results(
    items: object,
    *,
    url_key: str,
    title_key: str,
    description_key: str,
    policy: HostPolicy,
) -> list[SearchResult]:
    """Normalize a keyed provider's raw result list into ``SearchResult``s.

    The three keyed providers (Tavily/Brave/Serper) differ only in which key
    holds which field; the validation/dedupe/clip flow is one contract
    (jscpd #4 — it was hand-copied three times). Non-dict entries and
    non-public URLs are dropped, never rewritten.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = _normalize_url(str(item.get(url_key) or ""), policy=policy)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            SearchResult(
                url=url,
                title=_clip(str(item.get(title_key) or "")),
                description=_clip(str(item.get(description_key) or "")),
            )
        )
    return results


def _parse_json(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError) as error:
        raise SearchProviderError("provider returned non-JSON body") from error
    if not isinstance(payload, dict):
        raise SearchProviderError("provider returned a non-object JSON body")
    return payload


def _provider_message(provider: str, status: int, payload: dict[str, Any]) -> str:
    detail = payload.get("detail") or payload.get("error") or payload.get("message")
    if isinstance(detail, dict):
        detail = detail.get("message")
    return f"{provider} HTTP {status}" + (f": {detail}" if detail else "")


@dataclass(frozen=True)
class _ProviderCall:
    """One provider request, minus the credential.

    The method, endpoint, and payload are the only per-provider facts about
    going out over the wire; carrying them in one value is what lets
    :meth:`_ApiKeyProvider._request` be written once.
    """

    method: str
    url: str
    params: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


class _ApiKeyProvider:
    """Shared credential slot and shared flow for the keyed providers.

    The three providers differ only in endpoint, method, where the API key
    travels, and key names. Everything else -- issue the request, refuse a
    non-200 with the provider-specific message, normalize the result list --
    is this class, so a fourth provider cannot reach a result by forgetting the
    non-200 check, which is the one step of the flow that silently turns an
    error page into an empty result set.

    A subclass describes one query as :meth:`_call` and says where the raw
    result list sits as :attr:`_items_path`. It does not write the request: the
    per-provider ``_request`` override this replaced re-spelled the pooled call
    identically for every provider, so a provider added later could reach the
    network by a route the shared flow never sees.
    """

    name: str
    # Declared here so `_results` has one shape to read; assigned per provider.
    # A subclass that omits one fails loudly on the first result rather than
    # silently reporting a blank title for every hit.
    _url_key: str
    _title_key: str
    _description_key: str
    #: Keys walked from the parsed payload to the raw result list. Brave nests
    #: under ``web.results``, so this is a path rather than a single key.
    _items_path: tuple[str, ...]

    def __init__(self, api_key: str, host_policy: HostPolicy) -> None:
        self._api_key = api_key
        self._host_policy = host_policy

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        """Query this provider and return SSRF-screened, normalized results."""
        status, body = await self._request(query, limit, timeout_seconds)
        return self._results(status, body)

    def _call(self, query: str, limit: int) -> _ProviderCall:
        """This provider's request shape for one query. Subclass hook.

        The endpoint, the method, and whether the key travels in a header or
        the JSON body are per-provider facts, and none of them is a fact about
        what to do with the response.
        """
        raise NotImplementedError

    async def _request(self, query: str, limit: int, timeout_seconds: float) -> tuple[int, str]:
        """Issue this provider's request over the shared pooled client."""
        call = self._call(query, limit)
        return await _provider_http(
            call.method,
            call.url,
            params=call.params,
            json_body=call.json_body,
            headers=call.headers,
            timeout_seconds=timeout_seconds,
        )

    def _items_of(self, payload: dict[str, Any]) -> object:
        """Walk :attr:`_items_path` down to the raw result list.

        A missing or non-object node yields ``None`` rather than raising: the
        collector already reads anything that is not a list as no results, and
        a 200 with a differently-shaped body is an empty result set, not a
        crash.
        """
        node: object = payload
        for key in self._items_path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    def _results(self, status: int, body: str) -> list[SearchResult]:
        """The keyed-provider response contract, in one place.

        The three field names are read off the instance rather than passed in:
        they are per-provider class state, and taking them as arguments let a
        call site hand one provider's keys to another's collector.
        """
        payload = _parse_json(body)
        if status != 200:
            raise SearchProviderError(_provider_message(self.name, status, payload))
        return _collect_results(
            self._items_of(payload),
            url_key=self._url_key,
            title_key=self._title_key,
            description_key=self._description_key,
            policy=self._host_policy,
        )


class TavilyProvider(_ApiKeyProvider):
    """Tavily Search API (api.tavily.com). Free tier ~1000 credits/month."""

    name = "tavily"
    _url_key = "url"
    _title_key = "title"
    _description_key = "content"
    _items_path = ("results",)

    def _call(self, query: str, limit: int) -> _ProviderCall:
        # Tavily carries the key in the JSON body, not a header.
        return _ProviderCall(
            "POST",
            _TAVILY_URL,
            json_body={
                "api_key": self._api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
            headers={"Content-Type": "application/json"},
        )


class BraveProvider(_ApiKeyProvider):
    """Brave Search API (api.search.brave.com). Free tier 2000 queries/month."""

    name = "brave"
    _url_key = "url"
    _title_key = "title"
    _description_key = "description"
    _items_path = ("web", "results")

    def _call(self, query: str, limit: int) -> _ProviderCall:
        return _ProviderCall(
            "GET",
            _BRAVE_URL,
            params={"q": query, "count": limit, "safesearch": "off"},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )


class SerperProvider(_ApiKeyProvider):
    """Serper.dev Google SERP proxy. Free tier 2500 queries."""

    name = "serper"
    _url_key = "link"
    _title_key = "title"
    _description_key = "snippet"
    _items_path = ("organic",)

    def _call(self, query: str, limit: int) -> _ProviderCall:
        return _ProviderCall(
            "POST",
            _SERPER_URL,
            json_body={"q": query, "num": limit},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
        )


class DuckDuckGoProvider:
    """Keyless DuckDuckGo HTML endpoint (best-effort; captcha-prone from datacenters)."""

    name = "duckduckgo"

    def __init__(self, host_policy: HostPolicy) -> None:
        self._host_policy = host_policy

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        status, body = await _provider_http(
            "POST",
            _DUCKDUCKGO_URL,
            data={"q": query},
            headers={"User-Agent": _DUCKDUCKGO_UA},
            timeout_seconds=timeout_seconds,
        )
        if status != 200:
            raise SearchProviderError(
                f"duckduckgo HTTP {status}; the keyless provider is unreliable from "
                "datacenter IPs — configure a keyed provider via CRAWLUTRON_*_API_KEY"
            )
        lowered = body.lower()
        if "anomaly" in lowered or "challenge to confirm" in lowered:
            raise SearchProviderError(
                "duckduckgo returned a bot challenge; the keyless provider is unreliable "
                "from datacenter IPs — configure a keyed provider via CRAWLUTRON_*_API_KEY"
            )
        results: list[SearchResult] = []
        seen: set[str] = set()
        # The parse is the only thing that can fail here, and it is the only
        # thing that SHOULD fail here: this import used to sit inside the
        # `except Exception` below, so a missing lxml was reported to the caller
        # as "duckduckgo returned unparsable HTML" -- a provider's fault for a
        # broken local environment. lxml is a declared dependency.
        try:
            tree = lxml_html.fromstring(body)
        except Exception:
            raise SearchProviderError("duckduckgo returned unparsable HTML") from None
        for node in tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' result ')]"):
            link = node.xpath(".//a[contains(@class, 'result__a')]")
            if not link:
                continue
            url = _duckduckgo_target(
                str(link[0].get("href") or ""),
                policy=self._host_policy,
            )
            if not url:
                continue
            normalized = _normalize_url(url, policy=self._host_policy)
            if not normalized or normalized in seen:
                continue
            snippet_node = node.xpath(".//a[contains(@class, 'result__snippet')]") or node.xpath(
                ".//*[contains(@class, 'result__snippet')]"
            )
            seen.add(normalized)
            results.append(
                SearchResult(
                    url=normalized,
                    title=_clip(" ".join(link[0].text_content().split())),
                    description=_clip(" ".join(snippet_node[0].text_content().split()))
                    if snippet_node
                    else "",
                )
            )
            if len(results) >= limit:
                break
        return results


def _duckduckgo_target(href: str, *, policy: HostPolicy) -> str | None:
    """Resolve a DuckDuckGo result link to its real URL.

    DuckDuckGo wraps results as ``//duckduckgo.com/l/?uddg=<encoded>&rut=...``;
    extract and decode the ``uddg`` parameter when present.
    """
    if href.startswith(_DUCKDUCKGO_REDIRECT):
        parsed = urlsplit(href)
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return _normalize_url(unquote(uddg[0]), policy=policy)
        return None
    return _normalize_url(href, policy=policy)


def configured_search_providers(settings: Any) -> list[str]:
    """Names of providers that have the credentials they need, in priority order.

    ``auto`` selection walks this list and falls back to the keyless
    DuckDuckGo provider when it is empty.
    """
    names: list[str] = []
    if settings.tavily_api_key:
        names.append("tavily")
    if settings.brave_api_key:
        names.append("brave")
    if settings.serper_api_key:
        names.append("serper")
    return names


def active_search_provider_name(settings: Any) -> str:
    """The provider ``auto`` selection would pick for these settings."""
    providers = configured_search_providers(settings)
    return providers[0] if providers else "duckduckgo"


def get_search_provider(settings: Any) -> SearchProvider:
    """Resolve the configured search provider for ``settings``.

    Raises :class:`SearchProviderUnavailable` when an explicit provider was
    selected but its API key is missing, so the endpoint can fail with an
    actionable message instead of a generic 500.
    """
    requested = (settings.search_provider or "auto").strip().lower()
    host_policy = HostPolicy.from_settings(settings)
    available: dict[str, SearchProvider] = {
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
    "BraveProvider",
    "DuckDuckGoProvider",
    "SearchProvider",
    "SearchProviderError",
    "SearchProviderUnavailable",
    "SearchResult",
    "SerperProvider",
    "TavilyProvider",
    "active_search_provider_name",
    "configured_search_providers",
    "get_search_provider",
]
