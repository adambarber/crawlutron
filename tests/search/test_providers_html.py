"""The keyless DuckDuckGo provider: a scraped HTML page parsed and unwrapped.
No API key; the redirect wrapper ``//duckduckgo.com/l/?uddg=<enc>`` is unwrapped.
Provider HTTP is injected via ``provider_http``; no real network is touched.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from app.safety.validation import HostPolicy
from app.search.providers import SearchProviderError
from app.search.providers_html import DuckDuckGoProvider
from tests._assertions import awaitable_search


def test_parses_html_and_unwraps_redirect(fake_provider_http):
    target = "https://example.gov/agenda"
    wrapped = f"//duckduckgo.com/l/?uddg={quote(target, safe='')}&rut=abc"
    page = (
        '<html><body><div class="result">'
        f'<a class="result__a" href="{wrapped}">Agenda page</a>'
        '<a class="result__snippet">A snippet of the agenda.</a></div></body></html>'
    )
    fake_provider_http(200, page)
    provider = DuckDuckGoProvider(HostPolicy.unrestricted())
    results = awaitable_search(provider, "agenda", 5)
    assert [r.url for r in results] == [target]
    assert results[0].title == "Agenda page"
    assert "snippet" in results[0].description


def test_bot_challenge_raises(fake_provider_http):
    fake_provider_http(200, "<html>Anomaly detected. Please complete the following challenge.</html>")
    provider = DuckDuckGoProvider(HostPolicy.unrestricted())
    with pytest.raises(SearchProviderError, match="bot challenge"):
        awaitable_search(provider, "x", 5)


def test_non_200_status_raises(fake_provider_http):
    fake_provider_http(403, "Forbidden")
    provider = DuckDuckGoProvider(HostPolicy.unrestricted())
    with pytest.raises(SearchProviderError, match="HTTP 403"):
        awaitable_search(provider, "x", 5)
