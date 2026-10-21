"""The keyed search providers: one shared base plus Tavily, Brave and Serper.
Each differs only in endpoint, method and key placement; the response contract
and SSRF screening live in the base. The registry stays in ``providers.py``.
"""

from __future__ import annotations

from typing import Any

from app.safety.validation import HostPolicy
from app.search import providers as _providers
from app.search.providers import (
    BaseProvider,
    ProviderCall,
    SearchProviderError,
    SearchResult,
    collect_results,
    parse_provider_json,
    provider_message,
)

_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_SERPER_URL = "https://google.serper.dev/search"


class ApiKeyProvider(BaseProvider):
    """Shared credential slot and shared flow for the keyed providers, which
    differ only in endpoint, method and key placement."""

    name: str
    # Declared here so `_results` has one shape to read; a subclass that omits one
    # fails loudly on the first result rather than reporting a blank title.
    _url_key: str
    _title_key: str
    _description_key: str
    #: Keys walked from the parsed payload to the raw result list. Brave nests
    #: under ``web.results``, so this is a path rather than a single key.
    _items_path: tuple[str, ...]

    def __init__(self, api_key: str, host_policy: HostPolicy) -> None:
        super().__init__(host_policy)
        self._api_key = api_key

    def _call(self, query: str, limit: int) -> ProviderCall:
        """This provider's request shape for one query (subclass hook)."""
        raise NotImplementedError

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

    def _items_of(self, payload: dict[str, Any]) -> object:
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
            _BRAVE_URL,
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
            _SERPER_URL,
            json_body={"q": query, "num": limit},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
        )


__all__ = [
    "ApiKeyProvider",
    "BraveProvider",
    "SerperProvider",
    "TavilyProvider",
]
