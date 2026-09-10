"""POST /v1/search behavior: provider parsing, SSRF hygiene, selection, and the endpoint.

Provider HTTP is injected via ``app.search.providers._provider_http`` so no
real network is touched; the endpoint runs over the ASGI transport against the
real FastAPI app.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from app.search.providers import (
    BraveProvider,
    DuckDuckGoProvider,
    SearchProviderError,
    SearchProviderUnavailable,
    SerperProvider,
    TavilyProvider,
    _provider_http,
    configured_search_providers,
    get_search_provider,
)
from app.settings import CrawlerSettings


def _settings(**overrides) -> CrawlerSettings:
    merged = dict(os.environ)
    for key, value in overrides.items():
        merged[f"BROWSER_CRAWLER_{key.upper()}"] = str(value)
    return CrawlerSettings.from_mapping(merged)


# --- Provider parsing -------------------------------------------------------


class TestProviderParsing:
    async def test_tavily_parses_results_and_filters_private_urls(self, fake_provider_http, monkeypatch):
        # The suite defaults to private-targets ON for the loopback fixture
        # server; this test exercises the SSRF hygiene on results, so opt out.
        from _testkit import block_private_targets

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
        provider = TavilyProvider("test-key")
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
        provider = BraveProvider("test-key")
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
        provider = SerperProvider("test-key")
        results = await provider.search("minutes", 5, 10.0)
        assert results[0].url == "https://example.gov/s"
        assert results[0].title == "S"
        assert results[0].description == "s snippet"

    async def test_duckduckgo_parses_html_and_unwraps_redirect(self, fake_provider_http):
        from urllib.parse import quote

        target = "https://example.gov/agenda"
        wrapped = f"//duckduckgo.com/l/?uddg={quote(target, safe='')}&rut=abc"
        fake_provider_http(
            200,
            f"""
            <html><body>
              <div class="result">
                <a class="result__a" href="{wrapped}">Agenda page</a>
                <a class="result__snippet">A snippet of the agenda.</a>
              </div>
            </body></html>
            """,
        )
        provider = DuckDuckGoProvider()
        results = await provider.search("agenda", 5, 10.0)
        assert [r.url for r in results] == [target]
        assert results[0].title == "Agenda page"
        assert "snippet" in results[0].description

    async def test_duckduckgo_bot_challenge_raises(self, fake_provider_http):
        fake_provider_http(200, "<html>Anomaly detected. Please complete the following challenge.</html>")
        provider = DuckDuckGoProvider()
        with pytest.raises(SearchProviderError, match="bot challenge"):
            await provider.search("x", 5, 10.0)

    async def test_non_2xx_provider_status_raises_with_detail(self, fake_provider_http):
        fake_provider_http(401, json.dumps({"detail": "invalid api key"}))
        provider = TavilyProvider("bad-key")
        with pytest.raises(SearchProviderError, match="HTTP 401.*invalid api key"):
            await provider.search("x", 5, 10.0)

    async def test_non_json_body_raises(self, fake_provider_http):
        fake_provider_http(200, "not json at all")
        provider = TavilyProvider("key")
        with pytest.raises(SearchProviderError, match="non-JSON"):
            await provider.search("x", 5, 10.0)

    async def test_results_are_deduplicated_by_url(self, fake_provider_http):
        fake_provider_http(
            200,
            json.dumps(
                {
                    "results": [
                        {"title": "A", "url": "https://example.gov/a"},
                        {"title": "A dup", "url": "https://example.gov/a"},
                        {"title": "B", "url": "https://example.gov/b"},
                    ]
                }
            ),
        )
        provider = TavilyProvider("key")
        results = await provider.search("x", 5, 10.0)
        assert [r.url for r in results] == ["https://example.gov/a", "https://example.gov/b"]


# --- Provider selection -----------------------------------------------------


class TestProviderSelection:
    def test_auto_picks_first_configured_keyed_provider(self):
        settings = _settings(
            search_provider="auto", tavily_api_key="t", brave_api_key="b", serper_api_key="s"
        )
        assert get_search_provider(settings).name == "tavily"

    def test_auto_skips_unconfigured_to_next_keyed_provider(self):
        settings = _settings(search_provider="auto", brave_api_key="b")
        assert get_search_provider(settings).name == "brave"

    def test_auto_falls_back_to_duckduckgo_without_keys(self):
        settings = _settings(search_provider="auto")
        assert get_search_provider(settings).name == "duckduckgo"

    def test_explicit_provider_requires_its_key(self):
        settings = _settings(search_provider="tavily", tavily_api_key="t")
        assert get_search_provider(settings).name == "tavily"

    def test_explicit_unconfigured_provider_raises_actionable_error(self):
        settings = _settings(search_provider="brave")
        with pytest.raises(SearchProviderUnavailable, match="BROWSER_CRAWLER_BRAVE_API_KEY"):
            get_search_provider(settings)

    def test_configured_providers_report_priority_order(self):
        settings = _settings(
            search_provider="auto", tavily_api_key="t", serper_api_key="s", brave_api_key="b"
        )
        assert configured_search_providers(settings) == ["tavily", "brave", "serper"]


# --- Endpoint behavior ------------------------------------------------------


class TestSearchEndpoint:
    async def test_search_returns_shaped_results(self, async_client, override_settings, fake_provider_http):
        override_settings(search_provider="tavily", tavily_api_key="test-key")
        fake_provider_http(
            200,
            json.dumps({"results": [{"title": "A", "url": "https://example.gov/a", "content": "desc"}]}),
        )
        resp = await async_client.post("/v1/search", json={"query": "worcester", "limit": 3})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["provider"] == "tavily"
        assert body["query"] == "worcester"
        assert body["limit"] == 1
        assert body["data"][0] == {"url": "https://example.gov/a", "title": "A", "description": "desc"}

    async def test_search_clamps_limit_to_server_max(self, async_client, override_settings, monkeypatch):
        override_settings(search_provider="tavily", tavily_api_key="k", search_limit_max=3)
        capture: list = []

        async def _honoring_provider(method, url, **kwargs):
            asked = kwargs["json_body"]["max_results"]
            capture.append(kwargs)
            return 200, json.dumps(
                {"results": [{"title": f"R{i}", "url": f"https://example.gov/{i}"} for i in range(asked)]}
            )

        monkeypatch.setattr("app.search.providers._provider_http", _honoring_provider)
        resp = await async_client.post("/v1/search", json={"query": "x", "limit": 10})
        assert resp.status_code == 200
        assert capture[0]["json_body"]["max_results"] == 3
        assert len(resp.json()["data"]) == 3

    async def test_search_uses_default_limit_when_omitted(
        self, async_client, override_settings, fake_provider_http
    ):
        override_settings(search_provider="tavily", tavily_api_key="k", search_limit_default=2)
        capture: list = []
        fake_provider_http(
            200,
            json.dumps({"results": [{"title": "R", "url": f"https://example.gov/{i}"} for i in range(3)]}),
            capture=capture,
        )
        resp = await async_client.post("/v1/search", json={"query": "x"})
        assert resp.status_code == 200
        assert capture[0]["json_body"]["max_results"] == 2

    async def test_search_unconfigured_provider_returns_503(self, async_client, override_settings):
        override_settings(search_provider="brave")  # no BROWSER_CRAWLER_BRAVE_API_KEY
        resp = await async_client.post("/v1/search", json={"query": "x"})
        assert resp.status_code == 503
        assert resp.json()["code"] == "search_provider_unavailable"

    async def test_search_provider_error_returns_502(
        self, async_client, override_settings, fake_provider_http
    ):
        override_settings(search_provider="tavily", tavily_api_key="bad")
        fake_provider_http(401, json.dumps({"detail": "invalid api key"}))
        resp = await async_client.post("/v1/search", json={"query": "x"})
        assert resp.status_code == 502
        assert resp.json()["code"] == "search_provider_error"

    async def test_search_missing_query_is_rejected(self, async_client, override_settings):
        resp = await async_client.post("/v1/search", json={})
        assert resp.status_code == 422

    async def test_search_rejects_unknown_fields(self, async_client, override_settings):
        resp = await async_client.post("/v1/search", json={"query": "x", "bogus": 1})
        assert resp.status_code == 422

    async def test_search_records_metrics(self, async_client, override_settings, fake_provider_http):
        from app.metrics import get_metrics

        override_settings(search_provider="tavily", tavily_api_key="k")
        fake_provider_http(200, json.dumps({"results": [{"title": "A", "url": "https://example.gov/a"}]}))
        await async_client.post("/v1/search", json={"query": "x"})
        metrics = get_metrics()
        assert metrics.search_requests_total == 1
        assert metrics.search_results_total == 1
        assert metrics.search_provider_counts["tavily"] == 1

    async def test_search_endpoint_reports_active_provider_in_capabilities(
        self, async_client, override_settings
    ):
        override_settings(search_provider="auto", brave_api_key="b")
        resp = await async_client.get("/v1/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        assert body["features"]["web_search"] is True
        assert body["features"]["mcp"] is True
        assert body["search"]["active_provider"] == "brave"
        assert "brave" in body["search"]["configured_providers"]
        assert body["mcp"]["endpoint"] == "/mcp"


class TestProviderHttpCaps:
    """_provider_http must bound provider response bodies before parsing."""

    async def test_search_body_under_cap_passes_through(self):
        import app.search.providers as providers_mod

        small = json.dumps({"results": []}).encode()

        class _Body:
            async def aiter_bytes(self):
                yield small

        class _Resp:
            status_code = 200
            charset = "utf-8"

            def aiter_bytes(self):
                return _Body().aiter_bytes()

        class _Stream:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *args):
                return False

        class _Client:
            def stream(self, *a, **k):
                return _Stream()

        with mock.patch.object(providers_mod, "get_async_http_client", return_value=_Client()):
            status, body = await _provider_http("GET", "http://example.test/", timeout_seconds=5.0)
        assert status == 200
        assert json.loads(body) == {"results": []}

    async def test_search_unbounded_body_is_refused(self):
        import app.search.providers as providers_mod

        class _Body:
            async def aiter_bytes(self):
                while True:
                    yield b"x" * 65536

        class _Resp:
            status_code = 200
            charset = "utf-8"

            def aiter_bytes(self):
                return _Body().aiter_bytes()

        class _Stream:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *args):
                return False

        class _Client:
            def stream(self, *a, **k):
                return _Stream()

        with mock.patch.object(providers_mod, "get_async_http_client", return_value=_Client()):
            with pytest.raises(SearchProviderError, match="[Ee]xceed"):
                await _provider_http("GET", "http://example.test/", timeout_seconds=5.0)
