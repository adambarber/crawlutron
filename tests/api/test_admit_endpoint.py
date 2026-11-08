"""POST /v1/admit: the pre-flight admission contract, red-first for #654.

The base check (check_robots=false) is pure: zero I/O, zero quota, zero store
writes; the only I/O path is the opt-in robots check through the shared cache.
"""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from crawlutron.config.settings import CrawlerSettings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@asynccontextmanager
async def serve_admit(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    from tests._async_harnesses import serve

    async with serve(app) as client:
        yield client


def _admit_app(settings: CrawlerSettings | None = None) -> FastAPI:
    from crawlutron.api.admit_router import router as admit_router
    from crawlutron.api.dependencies import get_settings

    app = FastAPI()
    app.include_router(admit_router)
    app.dependency_overrides[get_settings] = lambda: settings or CrawlerSettings.from_mapping({})
    return app


@pytest.fixture
def admit_settings():
    return CrawlerSettings.from_mapping({})


async def _post_admit(client: AsyncClient, payload: dict[str, object]) -> dict[str, object]:
    return json.loads((await client.post("/v1/admit", json=payload)).text)


def _check(body: dict[str, object], key: str) -> object:
    checks = body["checks"]
    assert isinstance(checks, dict)
    return checks[key]


async def test_admitted_url_returns_canonical_url_and_clean_checks(admit_settings):
    async with serve_admit(_admit_app(admit_settings)) as client:
        body = await _post_admit(client, {"url": "https://Example.com:443/a?b=1"})
    assert body["admitted"] is True
    assert body["classification"] is None
    assert body["canonical_url"] == "https://example.com/a?b=1"
    assert body["checks"] == {"url": None, "host_policy": None, "egress": None, "robots": None}


class TestRefusedClassifications:
    """Each refusal surfaces as the existing classification vocabulary, never a 4xx."""

    async def test_invalid_url(self, admit_settings):
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "https:///no-host"})
        assert body["admitted"] is False
        assert body["classification"] == "invalid_url"
        assert _check(body, "url") == "invalid_url"
        assert body["canonical_url"] is None

    async def test_unsupported_scheme(self, admit_settings):
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "ftp://example.com/x"})
        assert body["admitted"] is False
        assert body["classification"] == "unsupported_scheme"
        assert _check(body, "url") == "unsupported_scheme"

    async def test_host_denied(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_DENIED_HOSTS": "blocked.test"})
        async with serve_admit(_admit_app(settings)) as client:
            body = await _post_admit(client, {"url": "https://blocked.test/secret"})
        assert body["admitted"] is False
        assert body["classification"] == "host_denied"
        assert _check(body, "host_policy") == "host_denied"
        assert _check(body, "url") is None

    async def test_host_not_allowed(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_ALLOWED_HOSTS": "allowed.test"})
        async with serve_admit(_admit_app(settings)) as client:
            body = await _post_admit(client, {"url": "https://elsewhere.test/"})
        assert body["admitted"] is False
        assert body["classification"] == "host_not_allowed"
        assert _check(body, "host_policy") == "host_not_allowed"

    async def test_private_network_target(self, admit_settings, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", raising=False)
        from crawlutron.models.host_policy import reset_private_target_memo

        reset_private_target_memo()
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "http://127.0.0.1:9/x"})
        assert body["admitted"] is False
        assert body["classification"] == "private_network_target"
        assert _check(body, "egress") == "private_network_target"


class TestPurity:
    """check_robots=false is 100% pure: no wire, no robots loader, no quota seam."""

    async def test_base_check_does_zero_io(self, monkeypatch, admit_settings):
        def _boom(*args, **kwargs):
            raise AssertionError("the pure admission check performed I/O")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _boom)
        monkeypatch.setattr("crawlutron.api.admit_endpoint.load_robots_rules", _boom)
        async with serve_admit(_admit_app(admit_settings)) as client:
            response = await client.post("/v1/admit", json={"url": "https://example.com/"})
        assert response.json()["admitted"] is True

    def test_endpoint_module_never_touches_the_quota_seam(self):
        from crawlutron.api import admit_endpoint

        source = inspect.getsource(admit_endpoint)
        assert "caller_auth" not in source
        assert "concurrency_tracker" not in source
        assert "Tracking" not in source


class TestRobotsOptIn:
    """check_robots=true is the only I/O path, through the shared robots cache."""

    @staticmethod
    def _loader_for(allowed: bool):
        from crawlutron.transport.robots import RobotsRules
        from crawlutron.vocabulary.robots import RobotsSource

        rules = [] if allowed else [(False, "/")]

        async def _fake_loader(target, fetcher, timeout_seconds, *, fetch_and_decode=None, policy=None):
            return RobotsRules(rules, [], None, RobotsSource.policy)

        return _fake_loader

    async def _admit_with_robots(self, monkeypatch, admit_settings, *, allowed: bool) -> dict[str, object]:
        """One shared harness: opt-in check with a scripted robots policy."""
        monkeypatch.setattr(
            "crawlutron.api.admit_endpoint.load_robots_rules", self._loader_for(allowed=allowed)
        )
        async with serve_admit(_admit_app(admit_settings)) as client:
            return await _post_admit(client, {"url": "https://example.com/x", "check_robots": True})

    async def test_disallowed_url(self, monkeypatch, admit_settings):
        body = await self._admit_with_robots(monkeypatch, admit_settings, allowed=False)
        assert body["admitted"] is False
        assert body["classification"] == "robots_disallowed"
        assert _check(body, "robots") == "robots_disallowed"
        assert body["canonical_url"] == "https://example.com/x"

    async def test_allowed_url_with_robots_check(self, monkeypatch, admit_settings):
        body = await self._admit_with_robots(monkeypatch, admit_settings, allowed=True)
        assert body["admitted"] is True
        assert _check(body, "robots") is None

    async def test_robots_check_is_opt_in(self, monkeypatch, admit_settings):
        calls: list[object] = []

        async def _loader(*args, **kwargs):
            calls.append(args)
            raise AssertionError("robots check ran without opt-in")

        monkeypatch.setattr("crawlutron.api.admit_endpoint.load_robots_rules", _loader)
        async with serve_admit(_admit_app(admit_settings)) as client:
            body = await _post_admit(client, {"url": "https://example.com/x"})
        assert body["admitted"] is True
        assert calls == []

    def test_robots_loader_is_the_shared_one(self):
        """The cache lives inside load_robots_rules; sharing it is sharing the function."""
        from crawlutron.api import admit_endpoint
        from crawlutron.transport.robots import load_robots_rules

        assert admit_endpoint.load_robots_rules is load_robots_rules


class TestMcpParity:
    """The MCP admit tool mirrors the REST handler one-for-one."""

    async def test_tool_list_advertises_admit(self):
        from crawlutron.mcp.server import tools_list_result

        tools = tools_list_result()["tools"]
        assert isinstance(tools, list)
        names = [tool["name"] for tool in tools if isinstance(tool, dict)]
        assert "admit" in names

    async def _tool_verdict(self, admit_settings, url: str) -> object:
        from crawlutron.mcp.handlers import handle_admit
        from crawlutron.models.admit_request import AdmitRequest

        result = await handle_admit(
            AdmitRequest(url=url),
            settings=admit_settings,
            shutdown_event=None,
            concurrency_tracker=None,
            job_store=None,
            caller=None,
        )
        structured = result.structured
        assert isinstance(structured, dict)
        return structured

    async def test_tool_mirrors_refused_rest_response(self, admit_settings):
        structured = await self._tool_verdict(admit_settings, "ftp://example.com/x")
        assert isinstance(structured, dict)
        assert structured["admitted"] is False
        assert structured["classification"] == "unsupported_scheme"

    async def test_tool_mirrors_admitted_rest_response(self, admit_settings):
        structured = await self._tool_verdict(admit_settings, "https://example.com/")
        assert isinstance(structured, dict)
        assert structured["admitted"] is True
        assert structured["canonical_url"] == "https://example.com/"


class TestDiscovery:
    """Discovery surfaces name admission in the same commit as the door."""

    def test_well_known_links_name_admit(self):
        from crawlutron.discovery.well_known import discovery_payload

        assert discovery_payload(CrawlerSettings.from_mapping({}))["links"]["admit"] == "/v1/admit"

    def test_recommended_endpoints_name_admit_before_crawl(self):
        from crawlutron.discovery.well_known import discovery_payload

        recommended = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        assert "POST /v1/admit" in recommended
        assert recommended.index("POST /v1/admit") < recommended.index("POST /v1/crawl")

    def test_capabilities_advertises_pre_flight_admission(self):
        from crawlutron.discovery.capabilities import capabilities_payload

        features = capabilities_payload(CrawlerSettings.from_mapping({}))["features"]
        assert features["pre_flight_admission"] is True

    def test_agent_hints_recommend_admission_as_preflight(self):
        from crawlutron.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "/v1/admit" in guidance
        assert "before the first request" in guidance
