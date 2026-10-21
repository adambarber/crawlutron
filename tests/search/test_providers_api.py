"""The keyed provider family: Tavily, Brave, Serper parsing and the folded base.
Each differs only in endpoint, method and key placement; the shared parsing and
SSRF screening are the base's job, covered by the ``test_search.py`` contract.
"""

from __future__ import annotations

import json

import pytest

from app.safety.validation import HostPolicy
from app.search.providers import ProviderCall, SearchProviderError
from app.search.providers_api import ApiKeyProvider, BraveProvider, SerperProvider, TavilyProvider
from tests._assertions import awaitable_search, block_private_targets, provider_results


class TestKeyedProviderParsing:
    async def test_tavily_parses_results_and_filters_private_urls(self, fake_provider_http, monkeypatch):
        # The suite defaults to private-targets ON for the loopback fixture server; this test exercises the
        # SSRF hygiene on results, so opt out.
        block_private_targets(monkeypatch)
        fake_provider_http(
            200,
            json.dumps(
                {
                    "results": [
                        {"title": "Good", "url": "https://example.gov/1", "content": "snippet one"},
                        {"title": "Private", "url": "http://127.0.0.1:8000/admin", "content": "sneaky"},
                        {"title": "No url", "content": "missing"},
                    ]
                }
            ),
        )
        provider = TavilyProvider("test-key", HostPolicy.unrestricted())
        results = await provider.search("worcester agenda", 5, 10.0)
        assert [r.url for r in results] == ["https://example.gov/1"]
        assert results[0].title == "Good"
        assert results[0].description == "snippet one"

    async def test_brave_parses_web_results(self, fake_provider_http):
        fake_provider_http(
            200,
            json.dumps(
                {
                    "web": {
                        "results": [
                            {"title": "B", "url": "https://example.gov/b", "description": "b desc"},
                            {"title": "B2", "url": "https://example.gov/b2", "description": "b2 desc"},
                        ]
                    }
                }
            ),
        )
        provider = BraveProvider("test-key", HostPolicy.unrestricted())
        results = await provider.search("records", 5, 10.0)
        assert [r.url for r in results] == ["https://example.gov/b", "https://example.gov/b2"]
        assert results[0].description == "b desc"

    async def test_serper_parses_organic_results(self, fake_provider_http):
        fake_provider_http(
            200,
            json.dumps(
                {
                    "organic": [
                        {"title": "S", "link": "https://example.gov/s", "snippet": "s snippet"},
                    ]
                }
            ),
        )
        provider = SerperProvider("test-key", HostPolicy.unrestricted())
        results = await provider.search("minutes", 5, 10.0)
        assert results[0].url == "https://example.gov/s"
        assert results[0].title == "S"
        assert results[0].description == "s snippet"

    async def test_keyed_providers_transmit_their_configured_key(self, fake_provider_http):
        """Each keyed provider sends the key it was constructed with."""
        capture = []
        fake_provider_http(200, json.dumps({"results": []}), capture=capture)
        await TavilyProvider("tavily-key", HostPolicy.unrestricted()).search("x", 1, 5.0)
        await BraveProvider("brave-key", HostPolicy.unrestricted()).search("x", 1, 5.0)
        await SerperProvider("serper-key", HostPolicy.unrestricted()).search("x", 1, 5.0)
        assert capture[0]["json_body"]["api_key"] == "tavily-key"
        assert capture[1]["headers"]["X-Subscription-Token"] == "brave-key"
        assert capture[2]["headers"]["X-API-KEY"] == "serper-key"

    async def test_non_2xx_provider_status_raises_with_detail(self, fake_provider_http):
        fake_provider_http(401, json.dumps({"detail": "invalid api key"}))
        provider = TavilyProvider("bad-key", HostPolicy.unrestricted())
        with pytest.raises(SearchProviderError, match="HTTP 401.*invalid api key"):
            await provider.search("x", 5, 10.0)

    async def test_non_json_body_raises(self, fake_provider_http):
        fake_provider_http(200, "not json at all")
        provider = TavilyProvider("key", HostPolicy.unrestricted())
        with pytest.raises(SearchProviderError, match="non-JSON"):
            await provider.search("x", 5, 10.0)

    async def test_results_are_deduplicated_by_url(self, fake_provider_http):
        fake_provider_http(
            200,
            provider_results(
                {"title": "A", "url": "https://example.gov/a"},
                {"title": "A dup", "url": "https://example.gov/a"},
                {"title": "B", "url": "https://example.gov/b"},
            ),
        )
        provider = TavilyProvider("key", HostPolicy.unrestricted())
        results = await provider.search("x", 5, 10.0)
        assert [r.url for r in results] == ["https://example.gov/a", "https://example.gov/b"]

    async def test_keyed_provider_clamps_results_to_caller_limit(self, fake_provider_http):
        # An over-emitting provider leaks nothing beyond the caller's ``limit``.
        too_many = [{"title": f"R{i}", "url": f"https://example.gov/{i}"} for i in range(7)]
        fake_provider_http(200, provider_results(*too_many))
        provider = TavilyProvider("key", HostPolicy.unrestricted())
        results = await provider.search("x", 3, 10.0)
        assert len(results) == 3
        assert [r.url for r in results] == [f"https://example.gov/{i}" for i in range(3)]

    async def test_non_2xx_non_json_body_surfaces_the_status(self, fake_provider_http):
        # An error body may be non-JSON (an HTML proxy page); surface the status.
        fake_provider_http(403, "<html>nope</html>")
        provider = TavilyProvider("key", HostPolicy.unrestricted())
        with pytest.raises(SearchProviderError, match="HTTP 403"):
            await provider.search("x", 3, 10.0)

    async def test_rate_limited_non_json_body_surfaces_the_status(self, fake_provider_http):
        fake_provider_http(429, "Rate limited")
        provider = TavilyProvider("key", HostPolicy.unrestricted())
        with pytest.raises(SearchProviderError, match="HTTP 429"):
            await provider.search("x", 3, 10.0)


class _MinimalProvider(ApiKeyProvider):
    """A fresh provider carrying only per-provider fields; no ``search``."""

    name = "minimal"
    _url_key = "loc"
    _title_key = "heading"
    _description_key = "blurb"
    _items_path = ("hits",)

    def _call(self, query: str, limit: int) -> ProviderCall:
        return ProviderCall("POST", "https://example.test/q", json_body={"q": query, "n": limit})


def test_keyed_provider_entry_is_folded_into_the_base():
    """None of the keyed providers re-implements ``search``: they all drive it."""
    for cls in (TavilyProvider, BraveProvider, SerperProvider):
        assert cls.search is ApiKeyProvider.search, f"{cls.__name__} re-implemented the entry"


def test_new_provider_with_only_per_provider_fields_works(fake_provider_http):
    """A provider that declares just its fields inherits and uses the folded entry."""
    provider = _MinimalProvider("secret-key", HostPolicy.unrestricted())

    fake_provider_http(
        200,
        json.dumps({"hits": [{"loc": "https://ok.example/a", "heading": "A", "blurb": "b"}]}),
    )
    results = awaitable_search(provider, "q", 5)

    assert len(results) == 1
    assert results[0].url == "https://ok.example/a"
    assert results[0].title == "A"
    assert results[0].description == "b"


def test_folded_base_validates_and_normalizes_the_same_for_every_provider(fake_provider_http):
    """The base's response contract runs identically for a minimal provider."""
    provider = _MinimalProvider("secret-key", HostPolicy.unrestricted())

    fake_provider_http(401, json.dumps({"detail": "bad"}))
    try:
        awaitable_search(provider, "q", 5)
    except SearchProviderError as error:
        assert "HTTP 401" in str(error)
        assert "bad" in str(error)
    else:  # pragma: no cover - the plain-return path is the assertion's inverse
        raise AssertionError("non-2xx status did not raise")
