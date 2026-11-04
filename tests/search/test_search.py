"""The shared search contract: ``provider_http``, ``collect_results``, the
registry, the endpoint skip and the #386 pin boundary."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from crawlutron.models.host_policy import HostPolicy
from crawlutron.search.providers import (
    PROVIDER_ENDPOINTS,
    SearchProviderError,
    SearchProviderUnavailable,
    collect_results,
    configured_search_providers,
)
from crawlutron.search.providers_api import BraveProvider, SerperProvider, TavilyProvider, get_search_provider
from crawlutron.search.providers_html import DuckDuckGoProvider
from tests._assertions import one_search_result, provider_results


def _collect(items):
    """collect_results at this file's uniform key names."""
    return collect_results(
        items,
        url_key="url",
        title_key="title",
        description_key="content",
        policy=HostPolicy.unrestricted(),
    )


# --- Shared result collector -------------------------------------------------


class TestCollectResults:
    def test_dedupes_by_normalized_url_and_preserves_order(self):
        # #520: path text is identity, so /a and /a/ are distinct values; exact
        # canonical duplicates (the two /b spellings) still dedupe to one.
        items = [
            {"url": "https://example.gov/a", "title": "A", "content": "first"},
            {"url": "https://example.gov/a/", "title": "A", "content": "dup"},
            {"url": "https://example.gov/b?utm_source=x", "title": "B", "content": "first"},
            {"url": "https://example.gov/b", "title": "B", "content": "dup"},
        ]
        results = _collect(items)
        assert [(r.url, r.title, r.description) for r in results] == [
            ("https://example.gov/a", "A", "first"),
            ("https://example.gov/a/", "A", "dup"),
            ("https://example.gov/b", "B", "first"),
        ]

    def test_skips_non_dicts_blank_and_private_urls(self, monkeypatch):
        from tests._assertions import block_private_targets

        block_private_targets(monkeypatch)
        items = [
            "not-a-dict",
            None,
            {"url": "", "title": "blank"},
            {"url": "http://127.0.0.1/secret", "title": "private"},
            {"title": "no url key"},
            {"url": "https://example.gov/ok", "title": "kept"},
        ]
        results = _collect(items)
        assert [r.url for r in results] == ["https://example.gov/ok"]
        assert results[0].description == ""

    def test_clips_whitespace_and_long_fields(self):
        from crawlutron.search.providers import MAX_TEXT_LENGTH

        results = _collect(
            [{"url": "https://example.gov/x", "title": " a  b ", "content": "z" * (MAX_TEXT_LENGTH + 50)}]
        )
        assert results[0].title == "a b"
        assert results[0].description.endswith("…")
        assert len(results[0].description) == MAX_TEXT_LENGTH + 1  # ellipsis adds one

    def test_none_items_iterable_is_empty(self):
        assert _collect(None) == []


# --- Provider selection -------------------------------------------------------


class TestProviderSelection:
    def test_auto_picks_first_configured_keyed_provider(self, override_settings):
        settings = override_settings(
            search_provider="auto", tavily_api_key="t", brave_api_key="b", serper_api_key="s"
        )
        assert get_search_provider(settings).name == "tavily"

    def test_auto_skips_unconfigured_to_next_keyed_provider(self, override_settings):
        settings = override_settings(search_provider="auto", brave_api_key="b")
        assert get_search_provider(settings).name == "brave"

    def test_auto_falls_back_to_duckduckgo_without_keys(self, override_settings):
        settings = override_settings(search_provider="auto")
        assert get_search_provider(settings).name == "duckduckgo"

    def test_explicit_provider_requires_its_key(self, override_settings):
        settings = override_settings(search_provider="tavily", tavily_api_key="t")
        assert get_search_provider(settings).name == "tavily"

    def test_explicit_unconfigured_provider_raises_actionable_error(self, override_settings):
        settings = override_settings(search_provider="brave")
        with pytest.raises(SearchProviderUnavailable, match="CRAWLUTRON_BRAVE_API_KEY"):
            get_search_provider(settings)

    def test_configured_providers_report_priority_order(self, override_settings):
        settings = override_settings(
            search_provider="auto", tavily_api_key="t", serper_api_key="s", brave_api_key="b"
        )
        assert configured_search_providers(settings) == ["tavily", "brave", "serper"]


# --- Endpoint behavior --------------------------------------------------------


class TestSearchEndpoint:
    async def test_search_returns_shaped_results(self, async_client, override_settings, fake_provider_http):
        one_search_result(override_settings, fake_provider_http)
        resp = await async_client.post("/v1/search", json={"query": "worcester", "limit": 3})
        body = resp.json()
        assert (resp.status_code, body["success"], body["provider"], body["query"]) == (
            200,
            True,
            "tavily",
            "worcester",
        )
        assert body["limit"] == 1
        assert body["data"][0] == {"url": "https://example.gov/a", "title": "A", "description": "desc"}

    async def test_search_clamps_limit_to_server_max(self, async_client, override_settings, monkeypatch):
        override_settings(search_provider="tavily", tavily_api_key="k", search_limit_max=3)
        capture = []

        async def _honoring_provider(method, url, **kwargs):
            capture.append(kwargs)
            asked = kwargs["json_body"]["max_results"]
            results = [{"title": f"R{i}", "url": f"https://example.gov/{i}"} for i in range(asked)]
            return 200, json.dumps({"results": results})

        monkeypatch.setattr("crawlutron.search.providers.provider_http", _honoring_provider)
        resp = await async_client.post("/v1/search", json={"query": "x", "limit": 10})
        assert (capture[0]["json_body"]["max_results"], len(resp.json()["data"])) == (3, 3)
        assert resp.status_code == 200

    async def test_search_uses_default_limit_when_omitted(
        self, async_client, override_settings, fake_provider_http
    ):
        override_settings(search_provider="tavily", tavily_api_key="k", search_limit_default=2)
        capture = []
        fake_provider_http(
            200,
            json.dumps({"results": [{"title": "R", "url": f"https://example.gov/{i}"} for i in range(3)]}),
            capture=capture,
        )
        await async_client.post("/v1/search", json={"query": "x"})
        assert capture[0]["json_body"]["max_results"] == 2

    async def test_search_unconfigured_provider_returns_503(self, async_client, override_settings):
        override_settings(search_provider="brave")  # no CRAWLUTRON_BRAVE_API_KEY
        resp = await async_client.post("/v1/search", json={"query": "x"})
        assert (resp.status_code, resp.json()["code"]) == (503, "search_provider_unavailable")

    async def test_search_provider_error_returns_502(
        self, async_client, override_settings, fake_provider_http
    ):
        override_settings(search_provider="tavily", tavily_api_key="bad")
        fake_provider_http(401, json.dumps({"detail": "invalid api key"}))
        resp = await async_client.post("/v1/search", json={"query": "x"})
        assert (resp.status_code, resp.json()["code"]) == (502, "search_provider_error")

    async def test_search_provider_network_failure_returns_error_envelope(
        self, async_client, override_settings, monkeypatch
    ):
        # A refused/DNS/read-timeout provider fault must stay in the envelope.
        import httpx

        import crawlutron.search.providers as providers_mod

        override_settings(search_provider="tavily", tavily_api_key="k")

        class _ConnRefused:
            def stream(self, *args, **kwargs):
                raise httpx.ConnectError("connection refused", request=None)

        monkeypatch.setattr(providers_mod, "get_async_http_client", lambda: _ConnRefused())
        resp = await async_client.post("/v1/search", json={"query": "x"})
        body = resp.json()
        assert (resp.status_code, body["code"]) == (502, "search_provider_error")
        assert set(body) == {"code", "message", "details"}

    async def test_search_missing_query_is_rejected(self, async_client, override_settings):
        assert (await async_client.post("/v1/search", json={})).status_code == 422

    async def test_search_rejects_unknown_fields(self, async_client, override_settings):
        resp = await async_client.post("/v1/search", json={"query": "x", "bogus": 1})
        assert resp.status_code == 422

    async def test_search_records_metrics(self, async_client, override_settings, fake_provider_http):
        from crawlutron.runtime.metrics import get_metrics

        override_settings(search_provider="tavily", tavily_api_key="k")
        fake_provider_http(200, provider_results({"title": "A", "url": "https://example.gov/a"}))
        await async_client.post("/v1/search", json={"query": "x"})
        metrics = get_metrics()
        assert (metrics.snapshot().search_requests_total, metrics.snapshot().search_results_total) == (1, 1)
        assert metrics.snapshot().search_provider_counts["tavily"] == 1

    async def test_search_endpoint_reports_active_provider_in_capabilities(
        self, async_client, override_settings
    ):
        override_settings(search_provider="auto", brave_api_key="b")
        body = (await async_client.get("/v1/capabilities")).json()
        assert (body["features"]["web_search"], body["features"]["mcp"]) == (True, True)
        assert body["search"]["active_provider"] == "brave"
        assert "brave" in body["search"]["configured_providers"]
        assert body["mcp"]["endpoint"] == "/mcp"


class TestProviderHttpCaps:
    """provider_http must bound provider response bodies before parsing."""

    async def test_search_body_under_cap_passes_through(self):
        import crawlutron.search.providers as providers_mod

        small = json.dumps({"results": []}).encode()
        client = _FakeStreamClient(_FakeStreamResponse(small))
        with mock.patch.object(providers_mod, "get_async_http_client", return_value=client):
            status, body = await providers_mod.provider_http(
                "GET", "http://example.test/", timeout_seconds=5.0
            )
        assert (status, json.loads(body)) == (200, {"results": []})

    async def test_search_unbounded_body_is_refused(self):
        import crawlutron.search.providers as providers_mod

        def _drip():
            while True:
                yield b"x" * 65536

        client = _FakeStreamClient(_FakeStreamResponse(_drip()))
        with mock.patch.object(providers_mod, "get_async_http_client", return_value=client):
            with pytest.raises(SearchProviderError, match="[Ee]xceed"):
                await providers_mod.provider_http("GET", "http://example.test/", timeout_seconds=5.0)


class _FakeStreamResponse:
    """httpx-faithful stream response: the attribute surface ``provider_http``
    relies on (``encoding``, ``status_code``, ``aiter_bytes``), chunks like httpx."""

    def __init__(self, body, encoding: str | None = "utf-8"):
        if isinstance(body, (bytes, bytearray)):
            body = [bytes(body)]
        self._chunks = body
        self.encoding = encoding
        self.status_code = 200

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamClient:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    def stream(self, method: str, url: str, **kwargs):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return outer._response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def _provider_decode(monkeypatch, body: bytes, encoding):
    """Drive the real ``provider_http`` decode path over a faked response."""
    import crawlutron.search.providers as providers_module

    fake = _FakeStreamClient(_FakeStreamResponse(body, encoding=encoding))
    monkeypatch.setattr(providers_module, "get_async_http_client", lambda: fake)
    return await providers_module.provider_http("GET", "http://provider.test/search", timeout_seconds=2.0)


async def test_provider_http_decodes_via_httpx_encoding(monkeypatch):
    """Real provider_http decode path: httpx exposes ``encoding``, not ``charset``."""
    status, text = await _provider_decode(monkeypatch, b'{"data": []}', "utf-8")
    assert status == 200
    assert text == '{"data": []}'


async def test_provider_http_decodes_utf8_when_encoding_unknown(monkeypatch):
    """No declared/detectable charset must fall back to UTF-8, not crash."""
    status, text = await _provider_decode(monkeypatch, b"<html>ok</html>", None)
    assert status == 200
    assert text == "<html>ok</html>"


# --- #386: provider fetches are pinned like the rest of the egress family ----


async def test_provider_registry_urls_are_not_caller_influenced(fake_provider_http):
    """The trust-boundary claim for the provider pin (#386): no caller input
    reaches the request URL. Every provider's search must target one of the
    fixed registry endpoints for any query, limit, and key."""
    capture = []
    fake_provider_http(200, "{}", capture=capture)
    for hostile_query in ("", "x" * 500, "https://evil.test/?q="):
        for limit in (1, 100):
            await TavilyProvider("k", HostPolicy.unrestricted()).search(hostile_query, limit, 5.0)
            await BraveProvider("k", HostPolicy.unrestricted()).search(hostile_query, limit, 5.0)
            await SerperProvider("k", HostPolicy.unrestricted()).search(hostile_query, limit, 5.0)
            await DuckDuckGoProvider(HostPolicy.unrestricted()).search(hostile_query, limit, 5.0)
    assert capture, "no provider call was observed"
    assert all(call["url"] in PROVIDER_ENDPOINTS for call in capture), [c["url"] for c in capture]


async def test_provider_http_pins_the_connect_to_the_resolved_ip(monkeypatch):
    """The provider fetch must connect to the IP it just validated (#377's
    answer, on our own egress too): a rebind can never move the connection."""
    import socket

    import crawlutron.search.providers as providers_module
    from tests._assertions import block_private_targets

    block_private_targets(monkeypatch)
    state = {"resolves": 0}

    def two_phase(host, *args, **kwargs):
        _ = (args, kwargs)
        state["resolves"] += 1
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("1.2.3.4" if state["resolves"] == 1 else "169.254.169.254", 80),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", two_phase)
    seen: list[str] = []

    class _RecordingClient(_FakeStreamClient):
        def stream(self, method, url, **kwargs):
            seen.append(url)
            return super().stream(method, url, **kwargs)

    client = _RecordingClient(_FakeStreamResponse(b"{}"))
    monkeypatch.setattr(providers_module, "get_async_http_client", lambda: client)
    await providers_module.provider_http("GET", "https://provider.test/search", timeout_seconds=5.0)
    assert seen == ["https://1.2.3.4/search"], "fetch was not pinned to the validated IP"
