"""Tests for extracted modular components."""

from app.crawl.decompression import decompress_body
from app.crawl.firecrawl_format import _extract_firecrawl_metadata, _text_from_html
from app.crawl.extraction import extract_content
from app.health.readiness import ReadinessState, create_health_router
from app.metrics import get_metrics
from app.discovery.capabilities import capabilities_payload, create_capabilities_router
from app.discovery.agent_hints import agent_hints_payload, create_agent_hints_router, EXAMPLE_POLICIES, STRATEGY_DESCRIPTIONS
from app.discovery.well_known import discovery_payload, create_well_known_router
from app.settings import CrawlerSettings


class TestDecompression:
    def test_gzip(self):
        import gzip
        data = gzip.compress(b"hello world")
        assert decompress_body(data, "gzip") == b"hello world"

    def test_deflate(self):
        import zlib
        data = zlib.compress(b"hello world")
        assert decompress_body(data, "deflate") == b"hello world"

    def test_no_encoding(self):
        assert decompress_body(b"hello", "") == b"hello"

    def test_unknown_encoding(self):
        assert decompress_body(b"hello", "unknown") == b"hello"


class TestFirecrawlFormat:
    def test_extract_firecrawl_metadata_handles_empty(self):
        meta = _extract_firecrawl_metadata("", "https://example.com")
        assert meta == {"source_url": "https://example.com"}

    def test_extract_content_html_returns_markdown(self):
        html = "<html><body><main>Content here</main></body></html>"
        result = extract_content(html, "text/html", "https://example.com")
        assert "Content here" in (result.markdown or "")

    def test_text_from_html_strips_tags(self):
        html = "<html><body><script>var x=1;</script><style>body{}</style><p>Hello</p></body></html>"
        text = _text_from_html(html)
        assert "Hello" in text
        assert "var x" not in text
        assert "body{}" not in text


class TestReadinessState:
    def test_initial_state_is_ready(self):
        state = ReadinessState()
        snap = state.snapshot()
        assert snap["status"] == "ready"
        assert snap["active_requests"] == 0
        assert snap["saturated"] is False

    def test_begin_end_request(self):
        state = ReadinessState()
        state.begin_request()
        snap = state.snapshot()
        assert snap["active_requests"] == 1
        assert snap["saturated"] is False
        state.end_request()
        snap = state.snapshot()
        assert snap["active_requests"] == 0

    def test_saturated_when_memory_at_ceiling(self):
        metrics = get_metrics()
        metrics.resource_memory_utilization = 0.0
        metrics.resource_cpu_utilization = 0.0
        state = ReadinessState()
        assert state.snapshot()["saturated"] is False
        # Memory gate rides at 0.92; at/above target the service is saturated.
        metrics.resource_memory_utilization = 0.95
        snap = state.snapshot()
        assert snap["saturated"] is True
        assert snap["status"] == "saturated"

    def test_saturated_when_cpu_at_ceiling(self):
        metrics = get_metrics()
        metrics.resource_memory_utilization = 0.0
        metrics.resource_cpu_utilization = 0.0
        state = ReadinessState()
        # CPU gate rides at 0.90; at/above target the service is saturated.
        metrics.resource_cpu_utilization = 0.93
        assert state.snapshot()["saturated"] is True

    def test_end_request_does_not_go_negative(self):
        state = ReadinessState()
        state.end_request()
        snap = state.snapshot()
        assert snap["active_requests"] == 0


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
