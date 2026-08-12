"""Tests for extracted modular components."""

import unittest
from app.crawl.decompression import decompress_body
from app.crawl.firecrawl_format import extract_firecrawl_format, _text_from_html
from app.health.readiness import ReadinessState, create_health_router
from app.discovery.capabilities import capabilities_payload, create_capabilities_router
from app.discovery.agent_hints import agent_hints_payload, create_agent_hints_router, EXAMPLE_POLICIES, STRATEGY_DESCRIPTIONS
from app.discovery.well_known import discovery_payload, create_well_known_router
from app.settings import CrawlerSettings


class DecompressionTest(unittest.TestCase):
    def test_gzip(self) -> None:
        import gzip
        data = gzip.compress(b"hello world")
        self.assertEqual(decompress_body(data, "gzip"), b"hello world")

    def test_deflate(self) -> None:
        import zlib
        data = zlib.compress(b"hello world")
        self.assertEqual(decompress_body(data, "deflate"), b"hello world")

    def test_no_encoding(self) -> None:
        self.assertEqual(decompress_body(b"hello", ""), b"hello")

    def test_unknown_encoding(self) -> None:
        self.assertEqual(decompress_body(b"hello", "unknown"), b"hello")


class FirecrawlFormatTest(unittest.TestCase):
    def test_empty_html_returns_none(self) -> None:
        md, meta = extract_firecrawl_format("", "https://example.com")
        self.assertIsNone(md)
        self.assertEqual(meta, {})

    def test_extracts_title_and_meta(self) -> None:
        html = """<html lang="en"><head><title>Test Page</title>
        <meta name="description" content="A test page">
        <meta property="og:title" content="OG Title">
        </head><body><main>Content here</main></body></html>"""
        md, meta = extract_firecrawl_format(html, "https://example.com")
        self.assertIsNotNone(md)
        self.assertEqual(meta["title"], "Test Page")
        self.assertEqual(meta["description"], "A test page")
        self.assertEqual(meta["og_title"], "OG Title")
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["source_url"], "https://example.com")

    def test_text_from_html_strips_tags(self) -> None:
        html = "<html><body><script>var x=1;</script><style>body{}</style><p>Hello</p></body></html>"
        text = _text_from_html(html)
        self.assertIn("Hello", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("body{}", text)


class ReadinessStateTest(unittest.TestCase):
    def test_initial_state_is_ready(self) -> None:
        state = ReadinessState(max_active_requests=2)
        snap = state.snapshot()
        self.assertEqual(snap["status"], "ready")
        self.assertEqual(snap["active_requests"], 0)
        self.assertFalse(snap["saturated"])

    def test_begin_end_request(self) -> None:
        state = ReadinessState(max_active_requests=2)
        state.begin_request()
        snap = state.snapshot()
        self.assertEqual(snap["active_requests"], 1)
        self.assertFalse(snap["saturated"])
        state.end_request()
        snap = state.snapshot()
        self.assertEqual(snap["active_requests"], 0)

    def test_saturation(self) -> None:
        state = ReadinessState(max_active_requests=1)
        state.begin_request()
        snap = state.snapshot()
        self.assertTrue(snap["saturated"])
        self.assertEqual(snap["status"], "saturated")

    def test_end_request_does_not_go_negative(self) -> None:
        state = ReadinessState(max_active_requests=1)
        state.end_request()
        snap = state.snapshot()
        self.assertEqual(snap["active_requests"], 0)


class CapabilitiesPayloadTest(unittest.TestCase):
    def test_capabilities_payload_structure(self) -> None:
        settings = CrawlerSettings.from_mapping()
        payload = capabilities_payload(settings)
        self.assertEqual(payload["service"], "browser-crawler")
        self.assertIn("supported_strategies", payload)
        self.assertIn("supported_classifications", payload)
        self.assertIn("output_fields", payload)
        self.assertIn("default_limits", payload)
        self.assertIn("features", payload)
        self.assertIn("openapi_url", payload)
        self.assertIn("swagger_ui_url", payload)

    def test_capabilities_payload_strategies(self) -> None:
        settings = CrawlerSettings.from_mapping()
        payload = capabilities_payload(settings)
        self.assertGreater(len(payload["supported_strategies"]), 0)
        self.assertIn("HTTP_DECLARED_CRAWLER", payload["supported_strategies"])


class AgentHintsPayloadTest(unittest.TestCase):
    def test_agent_hints_payload_structure(self) -> None:
        payload = agent_hints_payload()
        self.assertEqual(payload["service"], "browser-crawler")
        self.assertIn("strategies", payload)
        self.assertIn("guidance", payload)
        self.assertIn("example_policies", payload)

    def test_agent_hints_strategies_match(self) -> None:
        payload = agent_hints_payload()
        self.assertEqual(len(payload["strategies"]), len(STRATEGY_DESCRIPTIONS))
        for key, value in payload["strategies"].items():
            self.assertEqual(value, STRATEGY_DESCRIPTIONS.get(key))

    def test_example_policies_exist(self) -> None:
        payload = agent_hints_payload()
        self.assertGreater(len(payload["example_policies"]), 0)
        self.assertIn("browser_fallback", payload["example_policies"])


class DiscoveryPayloadTest(unittest.TestCase):
    def test_discovery_payload_structure(self) -> None:
        payload = discovery_payload()
        self.assertEqual(payload["service"], "browser-crawler")
        self.assertIn("version", payload)
        self.assertIn("policy_schema_version", payload)
        self.assertIn("links", payload)
        self.assertIn("recommended_endpoints", payload)
        self.assertIn("description", payload)

    def test_discovery_payload_links(self) -> None:
        payload = discovery_payload()
        self.assertIn("openapi", payload["links"])
        self.assertIn("capabilities", payload["links"])
        self.assertIn("agent_hints", payload["links"])


if __name__ == "__main__":
    unittest.main()
