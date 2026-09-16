"""Capabilities, agent-hints and well-known discovery payload contracts."""

from app.discovery.agent_hints import (
    EXAMPLE_POLICIES,
    STRATEGY_DESCRIPTIONS,
    agent_hints_payload,
    create_agent_hints_router,
)
from app.discovery.capabilities import capabilities_payload, create_capabilities_router
from app.discovery.well_known import create_well_known_router, discovery_payload
from app.settings import CrawlerSettings


class TestCapabilitiesPayload:
    def test_capabilities_payload_structure(self):
        settings = CrawlerSettings.from_mapping()
        payload = capabilities_payload(settings)
        assert payload["service"] == "browser-crawler"
        assert "supported_strategies" in payload
        assert "supported_classifications" in payload
        assert "output_fields" in payload
        assert "default_limits" in payload
        assert "features" in payload
        assert "openapi_url" in payload
        assert "swagger_ui_url" in payload

    def test_capabilities_payload_strategies(self):
        settings = CrawlerSettings.from_mapping()
        payload = capabilities_payload(settings)
        assert len(payload["supported_strategies"]) > 0
        assert "HTTP_DECLARED_CRAWLER" in payload["supported_strategies"]

    def test_capabilities_advertises_time_budget_and_async(self):
        settings = CrawlerSettings.from_mapping()
        payload = capabilities_payload(settings)
        assert payload["default_limits"]["total_url_budget_seconds"] > 0
        assert payload["features"]["async_mode"] is True
        timing = payload["crawl_timing"]
        assert timing["per_url_budget_seconds"] == payload["default_limits"]["total_url_budget_seconds"]
        assert "async_mode" in timing["large_crawl_guidance"]


class TestCapabilitiesWireContract:
    """Serialize through the real endpoint, not the raw dict.

    ``response_model=CapabilitiesResponse`` silently filters any top-level key the
    model does not declare; the payload-dict tests above cannot see that, so a new
    advertised block would ship green in tests yet be absent on the wire.
    """

    async def test_capabilities_endpoint_advertises_crawl_timing(self, async_client):
        resp = await async_client.get("/v1/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        timing = body.get("crawl_timing")
        assert timing is not None, "crawl_timing dropped by CapabilitiesResponse response_model"
        assert timing["per_url_budget_seconds"] == body["default_limits"]["total_url_budget_seconds"]
        assert timing["sync_has_server_time_ceiling"] is False
        assert "async_mode" in timing["large_crawl_guidance"]
        assert body["features"]["async_mode"] is True


class TestAgentHintsPayload:
    def test_agent_hints_payload_structure(self):
        payload = agent_hints_payload()
        assert payload["service"] == "browser-crawler"
        assert "strategies" in payload
        assert "guidance" in payload
        assert "example_policies" in payload

    def test_agent_hints_strategies_match(self):
        payload = agent_hints_payload()
        assert len(payload["strategies"]) == len(STRATEGY_DESCRIPTIONS)
        for key, value in payload["strategies"].items():
            assert value == STRATEGY_DESCRIPTIONS.get(key)

    def test_example_policies_exist(self):
        payload = agent_hints_payload()
        assert len(payload["example_policies"]) > 0
        assert "browser_fallback" in payload["example_policies"]

    def test_agent_hints_advertises_async_and_page_identity(self):
        payload = agent_hints_payload()
        guidance = " ".join(payload["guidance"]).lower()
        assert "async_mode" in guidance
        assert "same page" in guidance

    # EXAMPLE_POLICIES is imported by the module header; keep a reference so
    # linting does not flag the re-export while the payload tests cover content.
    _example_policies = EXAMPLE_POLICIES

    def _unused_health_router_import_guard(self):  # pragma: no cover
        return create_capabilities_router, create_agent_hints_router, create_well_known_router


class TestDiscoveryPayload:
    def test_discovery_payload_structure(self):
        payload = discovery_payload()
        assert payload["service"] == "browser-crawler"
        assert "version" in payload
        assert "policy_schema_version" in payload
        assert "links" in payload
        assert "recommended_endpoints" in payload
        assert "description" in payload

    def test_discovery_payload_links(self):
        payload = discovery_payload()
        assert "openapi" in payload["links"]
        assert "capabilities" in payload["links"]
        assert "agent_hints" in payload["links"]

    def test_discovery_advertises_async_and_job_surfaces(self):
        payload = discovery_payload()
        assert payload["links"]["result_retrieval"] == "/v1/crawl/{request_id}"
        assert payload["links"]["jobs"] == "/v1/jobs"
        assert payload["links"]["map"] == "/v1/map"
        recommended = " ".join(payload["recommended_endpoints"])
        assert "GET /v1/crawl/{request_id}" in recommended
        assert "POST /v1/jobs" in recommended
