"""Capabilities, agent-hints and well-known discovery payload contracts."""

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron_rest.discovery.agent_hints import (
    EXAMPLE_POLICIES,
    STRATEGY_DESCRIPTIONS,
    agent_hints_payload,
)
from crawlutron_rest.discovery.capabilities import capabilities_payload
from crawlutron_rest.discovery.well_known import discovery_payload


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

    def test_capabilities_pins_the_advertised_feature_key_set(self):
        """The /v1/capabilities feature dict is the contract agents key on, so its
        key set is pinned: an unexpected key fails the test, and so does a
        ``*_bypass`` key regressing (the ad-blocking break #30 fixed)."""
        settings = CrawlerSettings.from_mapping()
        features = capabilities_payload(settings)["features"]
        assert set(features) == {
            "headed_browser",
            "pdf_extraction",
            "direct_http",
            "trafilatura",
            "discovered_links",
            "frame_discovery",
            "form_discovery",
            "conditional_fetch",
            "per_request_trace",
            "policy_suggestions",
            "watches",
            "delta_crawl",
            "recursive_frontier",
            "warmup",
            "request_profiles",
            "browser_profiles",
            "web_search",
            "mcp",
            "pre_flight_admission",
            "async_mode",
            "captcha_solving",
            "proxy_rotation",
            "identity_rotation",
            "credential_injection",
            "robots_diagnostics",
            "sitemap_discovery",
            "feed_discovery",
            "adaptive_retry",
            "runtime_diagnostics",
            "domain_aware_concurrency",
            "readiness_endpoint",
            "event_loop_thread_offload",
        }
        assert not [k for k in features if "bypass" in k], features

    def test_large_crawl_guidance_routes_to_durable_jobs(self):
        """The no-ceiling note must name BOTH escape hatches.

        async_mode de-risks the HTTP request; only POST /v1/jobs is durable
        (pause/cancel/resume, crash recovery)."""
        settings = CrawlerSettings.from_mapping()
        timing = capabilities_payload(settings)["crawl_timing"]
        assert "/v1/jobs" in timing["large_crawl_guidance"]


class TestCapabilitiesWireContract:
    """Serialize through the real endpoint, not the raw dict.

    ``response_model`` silently filters undeclared top-level keys, which the
    payload-dict tests cannot see -- an unmodelled block would ship green."""

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
        """The no-time-ceiling guidance shipped as three hand-maintained copies.

        One constant now feeds capabilities, agent-hints and the OpenAPI
        description; this pins the shared identity ON THE WIRE."""
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

        Same rationale as the capabilities test: agents read the hints before
        the first request, so the durable option must be named there."""
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
            assert value == STRATEGY_DESCRIPTIONS[CrawlStrategy(key)]

    def test_example_policies_exist(self):
        payload = agent_hints_payload()
        assert len(payload["example_policies"]) > 0
        assert "browser_fallback" in payload["example_policies"]

    def test_agent_hints_advertises_async_and_page_identity(self):
        payload = agent_hints_payload()
        guidance = " ".join(payload["guidance"]).lower()
        assert "async_mode" in guidance
        assert "same page" in guidance

    # EXAMPLE_POLICIES is imported by the module header; keep a reference so linting does not flag the
    # re-export while the payload tests cover content.
    _example_policies = EXAMPLE_POLICIES


class TestDiscoveryPayload:
    def test_discovery_payload_structure(self):
        payload = discovery_payload(CrawlerSettings.from_mapping({}))
        assert payload["service"] == "crawlutron"
        assert "version" in payload
        assert "policy_schema_version" in payload
        assert "links" in payload
        assert "recommended_endpoints" in payload
        assert "description" in payload

    def test_discovery_payload_links(self):
        payload = discovery_payload(CrawlerSettings.from_mapping({}))
        assert "openapi" in payload["links"]
        assert "capabilities" in payload["links"]
        assert "agent_hints" in payload["links"]

    def test_discovery_advertises_async_and_job_surfaces(self):
        payload = discovery_payload(CrawlerSettings.from_mapping({}))
        assert payload["links"]["result_retrieval"] == "/v1/crawl/{request_id}"
        assert payload["links"]["jobs"] == "/v1/jobs"
        assert payload["links"]["map"] == "/v1/map"
        recommended = " ".join(payload["recommended_endpoints"])
        assert "GET /v1/crawl/{request_id}" in recommended
        assert "POST /v1/jobs" in recommended

    def test_recommended_endpoints_name_the_job_control_pair(self):
        """An agent that discovers the service the documented way can start a
        job and read its state, but could not stop one: the control and delete
        endpoints were live but absent from the recommendation list."""
        recommended = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        assert "POST /v1/jobs/{job_id}/control" in recommended
        assert "DELETE /v1/jobs/{job_id}" in recommended

    def test_recommended_endpoints_name_the_watch_surface(self):
        """A watch is only useful to an agent that can register one and then read
        what the sweep concluded: both halves were live in the router and absent
        from the list a discovering agent actually reads."""
        recommended = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        assert "POST /v1/watches" in recommended
        assert "GET /v1/watches" in recommended
        assert "GET /v1/watches/{watch_id}" in recommended
        assert "DELETE /v1/watches/{watch_id}" in recommended

    def test_recommended_endpoints_name_the_trace_view(self):
        """A trace nobody can find is not shipped: the door an agent reads first
        is the discovery list, not the OpenAPI document."""
        from crawlutron_rest.discovery.well_known import discovery_payload

        recommended = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        assert "GET /v1/crawl/{request_id}/trace" in recommended

    def test_recommended_endpoints_name_the_policy_suggestion_surface(self):
        """A suggestion surface an agent cannot discover is not a surface: the
        guidance already tells agents to generate reviewable policy suggestions,
        and this is the door that does it."""
        recommended = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        assert "GET /v1/policy-suggestions" in recommended

    def test_capabilities_advertises_watches(self):
        """``conditional_fetch`` is the primitive; ``watches`` is the surface that
        uses it on the caller's behalf. An agent deciding whether to poll for
        changes has to be able to see that the service will."""
        from crawlutron_rest.discovery.capabilities import capabilities_payload

        features = capabilities_payload(CrawlerSettings.from_mapping({}))["features"]
        assert features["watches"] is True

    def test_every_recommended_endpoint_is_a_live_route(self):
        """The recommendation list is hand-curated, so it can drift from the
        routers it mirrors: an entry for a dead route sends a discovering agent
        at a 404. Every advertised METHOD /path must resolve against the app."""
        from crawlutron_rest.main import app as service

        # The app mounts its routers via _IncludedRouter, so app.routes does not expose APIRoute objects; the
        # OpenAPI schema is the surface the recommendation list mirrors and the one a discovering agent
        assert service is not None, "the lifespan-set app is absent; re-ground this walk"
        live = {
            f"{method.upper()} {route}"
            for route, operations in service.openapi()["paths"].items()
            for method in operations
        }
        advertised = discovery_payload(CrawlerSettings.from_mapping({}))["recommended_endpoints"]
        stale = [entry for entry in advertised if entry not in live]
        assert not stale, f"recommended_endpoints name routes the app does not serve: {stale}"


class TestOperatorSeal:
    """is_operator_sealed reports whether the keys surface refuses everything."""

    def test_no_token_means_sealed(self):
        payload = discovery_payload(CrawlerSettings.from_mapping({}))
        assert payload["is_operator_sealed"] is True

    def test_token_set_means_open(self):
        payload = discovery_payload(CrawlerSettings.from_mapping({"CRAWLUTRON_KEY_ADMIN_TOKEN": "secret"}))
        assert payload["is_operator_sealed"] is False

    async def test_endpoint_reflects_the_seal(self, async_client, monkeypatch):
        from crawlutron_rest.api.dependencies import get_settings

        app = async_client._transport.app
        app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
        try:
            body = (await async_client.get("/.well-known/crawler-service")).json()
            assert body["is_operator_sealed"] is True
        finally:
            app.dependency_overrides.pop(get_settings, None)
