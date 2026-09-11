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

from app.bounded_io import BodyTooLarge, read_capped_async
from app.crawl.strategies.http import get_async_http_client
from app.timeouts import Timeouts
from app.validation import normalize_public_http_url

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


def _normalize_url(raw: str) -> str | None:
    """Return ``raw`` when it is a crawlable public HTTP(S) URL, else ``None``.

    Dropping (rather than rewriting) invalid targets keeps the response honest:
    a result that points at loopback or a private network is not a public URL
    a caller can legally crawl, so it is not advertised.
    """
    try:
        return normalize_public_http_url(raw)
    except ValueError:
        return None


def _collect_results(
    items: object, *, url_key: str, title_key: str, description_key: str
) -> list[SearchResult]:
    """Normalize a keyed provider's raw result list into ``SearchResult``s.

    The three keyed providers (Tavily/Brave/Serper) differ only in which key
    holds which field; the validation/dedupe/clip flow is one contract
    (jscpd #4 — it was hand-copied three times). Non-dict entries and
    non-public URLs are dropped, never rewritten (#1721).
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = _normalize_url(str(item.get(url_key) or ""))
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


class TavilyProvider:
    """Tavily Search API (api.tavily.com). Free tier ~1000 credits/month."""

    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        status, body = await _provider_http(
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
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_json(body)
        if status != 200:
            raise SearchProviderError(_provider_message(self.name, status, payload))
        return _collect_results(payload.get("results"), url_key="url", title_key="title", description_key="content")


class BraveProvider:
    """Brave Search API (api.search.brave.com). Free tier 2000 queries/month."""

    name = "brave"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        status, body = await _provider_http(
            "GET",
            _BRAVE_URL,
            params={"q": query, "count": limit, "safesearch": "off"},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_json(body)
        if status != 200:
            raise SearchProviderError(_provider_message(self.name, status, payload))
        return _collect_results(
            (payload.get("web") or {}).get("results"), url_key="url", title_key="title", description_key="description"
        )


class SerperProvider:
    """Serper.dev Google SERP proxy. Free tier 2500 queries."""

    name = "serper"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(self, query: str, limit: int, timeout_seconds: float) -> list[SearchResult]:
        status, body = await _provider_http(
            "POST",
            _SERPER_URL,
            json_body={"q": query, "num": limit},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_json(body)
        if status != 200:
            raise SearchProviderError(_provider_message(self.name, status, payload))
        return _collect_results(payload.get("organic"), url_key="link", title_key="title", description_key="snippet")


class DuckDuckGoProvider:
    """Keyless DuckDuckGo HTML endpoint (best-effort; captcha-prone from datacenters)."""

    name = "duckduckgo"

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
                "datacenter IPs — configure a keyed provider via BROWSER_CRAWLER_*_API_KEY"
            )
        lowered = body.lower()
        if "anomaly" in lowered or "challenge to confirm" in lowered:
            raise SearchProviderError(
                "duckduckgo returned a bot challenge; the keyless provider is unreliable "
                "from datacenter IPs — configure a keyed provider via BROWSER_CRAWLER_*_API_KEY"
            )
        results: list[SearchResult] = []
        seen: set[str] = set()
        try:
            from lxml import html as lxml_html

            tree = lxml_html.fromstring(body)
        except Exception:
            raise SearchProviderError("duckduckgo returned unparsable HTML") from None
        for node in tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' result ')]"):
            link = node.xpath(".//a[contains(@class, 'result__a')]")
            if not link:
                continue
            url = _duckduckgo_target(str(link[0].get("href") or ""))
            if not url:
                continue
            normalized = _normalize_url(url)
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


def _duckduckgo_target(href: str) -> str | None:
    """Resolve a DuckDuckGo result link to its real URL.

    DuckDuckGo wraps results as ``//duckduckgo.com/l/?uddg=<encoded>&rut=...``;
    extract and decode the ``uddg`` parameter when present.
    """
    if href.startswith(_DUCKDUCKGO_REDIRECT):
        parsed = urlsplit(href)
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
        return None
    return href


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
    available: dict[str, SearchProvider] = {
        provider.name: provider
        for provider in (
            TavilyProvider(settings.tavily_api_key) if settings.tavily_api_key else None,
            BraveProvider(settings.brave_api_key) if settings.brave_api_key else None,
            SerperProvider(settings.serper_api_key) if settings.serper_api_key else None,
        )
        if provider is not None
    }
    if requested == "auto":
        return next(iter(available.values())) if available else DuckDuckGoProvider()
    if requested == "duckduckgo":
        return DuckDuckGoProvider()
    provider = available.get(requested)
    if provider is not None:
        return provider
    raise SearchProviderUnavailable(
        f"search provider '{requested}' is not configured; "
        f"set BROWSER_CRAWLER_{requested.upper()}_API_KEY (or BROWSER_CRAWLER_SEARCH_PROVIDER=duckduckgo)"
    )


__all__ = [
    "SearchResult",
    "SearchProvider",
    "SearchProviderError",
    "SearchProviderUnavailable",
    "TavilyProvider",
    "BraveProvider",
    "SerperProvider",
    "DuckDuckGoProvider",
    "get_search_provider",
    "configured_search_providers",
    "active_search_provider_name",
]
