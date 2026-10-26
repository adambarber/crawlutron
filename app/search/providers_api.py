"""The keyed search providers: one shared base plus Tavily, Brave and Serper.
Each differs only in endpoint, method and key placement; the response contract
and SSRF screening live in the base. The registry stays in ``providers.py``.
"""

from __future__ import annotations

import abc
from typing import Any

from app.models.host_policy import HostPolicy
from app.models.json_value import JsonValue
from app.search import providers as _providers
from app.search.providers import (
    BRAVE_URL,
    SERPER_URL,
    TAVILY_URL,
    BaseProvider,
    ProviderCall,
    SearchProviderError,
    SearchProviderUnavailable,
    SearchResult,
    collect_results,
    parse_provider_json,
    provider_message,
)
from app.search.providers_html import DuckDuckGoProvider


class ApiKeyProvider(BaseProvider):
    """Shared credential slot and shared flow for the keyed providers, which
    differ only in endpoint, method and key placement."""

    name: str
    # Field declarations are data, not hooks; the duplication rule pins them.
    _url_key: str
    _title_key: str
    _description_key: str
    #: Keys walked from the parsed payload to the raw result list. Brave nests
    #: under ``web.results``, so this is a path rather than a single key.
    _items_path: tuple[str, ...]

    def __init__(self, api_key: str, host_policy: HostPolicy) -> None:
        super().__init__(host_policy)
        self._api_key = api_key

    @abc.abstractmethod
    def _call(self, query: str, limit: int) -> ProviderCall:
        """This provider's request shape for one query (subclass hook)."""

    async def _fetch(self, query: str, limit: int, timeout_seconds: float) -> tuple[int, str]:
        """Issue this provider's request over the shared pooled client."""
        call = self._call(query, limit)
        return await _providers.provider_http(
            call.method,
            call.url,
            params=call.params,
            json_body=call.json_body,
            headers=call.headers,
            timeout_seconds=timeout_seconds,
        )

    def _items_of(self, payload: dict[str, JsonValue]) -> object:
        """Walk :attr:`_items_path` down to the raw result list.
        A missing or non-object node yields ``None``, not a raise: the collector
        reads a non-list as no results, so a shaped-unexpected 200 is empty.
        """
        node: object = payload
        for key in self._items_path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    def _results(self, status: int, body: str, limit: int) -> list[SearchResult]:
        """The keyed-provider response contract: refuse non-200, parse, normalize
        and clamp to the caller's ``limit`` (an over-emitter leaks nothing)."""
        if status != 200:
            # The status is decided before the body is trusted: a non-2xx body
            # may be non-JSON, so a blind parse must never swallow it.
            try:
                detail = parse_provider_json(body)
            except SearchProviderError:
                detail = {}
            raise SearchProviderError(provider_message(self.name, status, detail))
        payload = parse_provider_json(body)
        return collect_results(
            self._items_of(payload),
            url_key=self._url_key,
            title_key=self._title_key,
            description_key=self._description_key,
            policy=self._host_policy,
        )[:limit]


class TavilyProvider(ApiKeyProvider):
    """Tavily Search API (api.tavily.com). Free tier ~1000 credits/month."""

    name = "tavily"
    _url_key = "url"
    _title_key = "title"
    _description_key = "content"
    _items_path = ("results",)

    def _call(self, query: str, limit: int) -> ProviderCall:
        # Tavily carries the key in the JSON body, not a header.
        return ProviderCall(
            "POST",
            TAVILY_URL,
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


class BraveProvider(ApiKeyProvider):
    """Brave Search API (api.search.brave.com). Free tier 2000 queries/month."""

    name = "brave"
    _url_key = "url"
    _title_key = "title"
    _description_key = "description"
    _items_path = ("web", "results")

    def _call(self, query: str, limit: int) -> ProviderCall:
        return ProviderCall(
            "GET",
            BRAVE_URL,
            params={"q": query, "count": limit, "safesearch": "off"},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )


class SerperProvider(ApiKeyProvider):
    """Serper.dev Google SERP proxy. Free tier 2500 queries."""

    name = "serper"
    _url_key = "link"
    _title_key = "title"
    _description_key = "snippet"
    _items_path = ("organic",)

    def _call(self, query: str, limit: int) -> ProviderCall:
        return ProviderCall(
            "POST",
            SERPER_URL,
            json_body={"q": query, "num": limit},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
        )


def get_search_provider(settings: Any) -> BaseProvider:
    """Resolve the configured search provider; raise
    :class:`SearchProviderUnavailable` when an explicit provider was selected
    but its API key is missing. Lives here: it constructs this family."""
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
    "ApiKeyProvider",
    "BraveProvider",
    "SerperProvider",
    "TavilyProvider",
    "get_search_provider",
]
