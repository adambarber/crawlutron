"""The search-provider entry fold is complete: the base drives every provider (#128).

The keyed providers declare only per-provider fields; the folded entry lives in the base.
"""

import json

from app.safety.validation import HostPolicy
from app.search.providers import (
    BraveProvider,
    SearchProviderError,
    SerperProvider,
    TavilyProvider,
    _ApiKeyProvider,
    _ProviderCall,
)


def test_keyed_provider_entry_is_folded_into_the_base():
    """None of the keyed providers re-implements ``search``: they all drive it."""
    for cls in (TavilyProvider, BraveProvider, SerperProvider):
        assert cls.search is _ApiKeyProvider.search, f"{cls.__name__} re-implemented the entry"


class _MinimalProvider(_ApiKeyProvider):
    """A fresh provider carrying only per-provider fields; no ``search``."""

    name = "minimal"
    _url_key = "loc"
    _title_key = "heading"
    _description_key = "blurb"
    _items_path = ("hits",)

    def _call(self, query: str, limit: int) -> _ProviderCall:
        return _ProviderCall("POST", "https://example.test/q", json_body={"q": query, "n": limit})


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


def awaitable_search(provider, query: str, limit: int):
    import asyncio

    return asyncio.run(provider.search(query, limit, timeout_seconds=1.0))
