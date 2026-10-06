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
        assert payload["service"] == "crawlutron"
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

    def test_large_crawl_guidance_routes_to_durable_jobs(self):
        """The no-ceiling note must name BOTH escape hatches.

        async_mode de-risks the HTTP request; only POST /v1/jobs is durable
        (pause/cancel/resume, crash recovery). An agent that reads only
        async_mode picks a continuation of a single request when it should
        book a job -- the guidance is the only place this choice is written.
        """
        settings = CrawlerSettings.from_mapping()
        timing = capabilities_payload(settings)["crawl_timing"]
        assert "/v1/jobs" in timing["large_crawl_guidance"]


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
        assert "/v1/jobs" in timing["large_crawl_guidance"]
        assert body["features"]["async_mode"] is True

    async def test_timing_guidance_is_one_string_shared_by_every_surface(self, async_client):
        """The no-time-ceiling guidance shipped as three
        hand-maintained copies (capabilities, agent-hints, the OpenAPI
        endpoint description). One constant now feeds all three, so the
        surfaces cannot drift; this pins the identity on the WIRE,
        not in the source text.
        """
        caps = (await async_client.get("/v1/capabilities")).json()
        guidance = caps["crawl_timing"]["large_crawl_guidance"]
        hints = (await async_client.get("/v1/agent-hints")).json()
        assert guidance in hints["guidance"], "agent-hints drifted from the shared guidance constant"
        openapi = (await async_client.get("/openapi.json")).json()
        description = openapi["paths"]["/v1/crawl"]["post"]["description"]
        assert guidance in description, "OpenAPI description drifted from the shared guidance constant"
        # Routing content survives the unification (behavior, not wording).
        assert "no server-side time ceiling" in guidance
        assert "async_mode" in guidance and "/v1/jobs" in guidance


class TestAgentHintsPayload:
    def test_agent_hints_payload_structure(self):
        payload = agent_hints_payload()
        assert payload["service"] == "crawlutron"
        assert "strategies" in payload
        assert "guidance" in payload
        assert "example_policies" in payload

    def test_agent_hints_time_guidance_names_both_escape_hatches(self):
        """The no-ceiling hint must point at async_mode AND jobs.

        Same rationale as the capabilities guidance test: the hints are what
        agents actually read before constructing a first request, so the
        durable option must be named where the sync ceiling is documented.
        """
        payload = agent_hints_payload()
        timing_hints = [g for g in payload["guidance"] if "time ceiling" in g]
        assert timing_hints, "no time-ceiling guidance in agent hints"
        hint = timing_hints[0]
        assert "async_mode" in hint
        assert "/v1/jobs" in hint

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
        assert payload["service"] == "crawlutron"
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

    def test_recommended_endpoints_name_the_job_control_pair(self):
        """An agent that discovers the service the documented way can start a
        job and read its state, but could not stop one: the control and delete
        endpoints were live in the router and absent from the recommendation
        list, which is the surface a discovering agent reads."""
        recommended = discovery_payload()["recommended_endpoints"]
        assert "POST /v1/jobs/{job_id}/control" in recommended
        assert "DELETE /v1/jobs/{job_id}" in recommended

    def test_every_recommended_endpoint_is_a_live_route(self):
        """The recommendation list is hand-curated, so it can drift from the
        routers it mirrors: an entry for a route that no longer exists sends a
        discovering agent at a 404 with no signal the list is stale. Every
        advertised METHOD /path must resolve against the real app."""
        from app.main import app as service

        # The app mounts its routers via _IncludedRouter, so app.routes does
        # not expose APIRoute objects; the OpenAPI schema is the surface the
        # recommendation list mirrors and the one a discovering agent resolves
        # against.
        live = {
            f"{method.upper()} {route}"
            for route, operations in service.openapi()["paths"].items()
            for method in operations
        }
        advertised = discovery_payload()["recommended_endpoints"]
        stale = [entry for entry in advertised if entry not in live]
        assert not stale, f"recommended_endpoints name routes the app does not serve: {stale}"
