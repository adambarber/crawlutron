"""Payload helpers: policy construction, advertised limits and invalid-limit handling."""

from _testkit import *

from app.crawl.payload_helpers import _payload_policy
from app.settings import CrawlerSettings


class TestPayloadPolicyCarriesAdvertisedLimits:
    def test_max_rps_survives_normalization(self):
        policy = _payload_policy(
            {"url": "https://example.com/", "policy": {"limits": {"max_rps": 2.5}}},
            CrawlerSettings.from_mapping({}),
        )
        assert policy["limits"]["max_rps"] == 2.5

    def test_max_rps_default_is_zero(self):
        policy = _payload_policy(
            {"url": "https://example.com/"},
            CrawlerSettings.from_mapping({}),
        )
        assert policy["limits"]["max_rps"] == 0.0

    def test_max_rps_out_of_range_rejected(self):
        # CrawlPolicyLimits bounds max_rps to [0, 100]; out-of-range requests
        # must be rejected, not silently clamped into a different contract.
        import pytest

        with pytest.raises(ValueError):
            _payload_policy(
                {"url": "https://example.com/", "policy": {"limits": {"max_rps": 500}}},
                CrawlerSettings.from_mapping({}),
            )

    def test_max_content_length_bytes_survives_normalization(self):
        policy = _payload_policy(
            {"url": "https://example.com/", "policy": {"limits": {"max_content_length_bytes": 1024}}},
            CrawlerSettings.from_mapping({}),
        )
        assert policy["limits"]["max_content_length_bytes"] == 1024

    def test_max_content_length_above_ceiling_rejected(self):
        # An explicit cap must not be a caller-controlled RAM/decompression
        # override: over the hard ceiling it is rejected, like max_rps, never
        # silently clamped into a different contract (M2).
        import pytest

        from app.constants import MAX_CONTENT_LENGTH_BYTES

        with pytest.raises(ValueError):
            _payload_policy(
                {
                    "url": "https://example.com/",
                    "policy": {"limits": {"max_content_length_bytes": MAX_CONTENT_LENGTH_BYTES + 1}},
                },
                CrawlerSettings.from_mapping({}),
            )

    def test_max_content_length_at_ceiling_survives(self):
        from app.constants import MAX_CONTENT_LENGTH_BYTES

        policy = _payload_policy(
            {
                "url": "https://example.com/",
                "policy": {"limits": {"max_content_length_bytes": MAX_CONTENT_LENGTH_BYTES}},
            },
            CrawlerSettings.from_mapping({}),
        )
        assert policy["limits"]["max_content_length_bytes"] == MAX_CONTENT_LENGTH_BYTES

    def test_max_content_length_above_ceiling_is_a_validation_error(self):
        # The pydantic boundary must 422 oversized caps (le= on the model).
        import pytest
        from pydantic import ValidationError

        from app.api_models import CrawlPolicyLimits
        from app.constants import MAX_CONTENT_LENGTH_BYTES

        with pytest.raises(ValidationError):
            CrawlPolicyLimits(max_content_length_bytes=MAX_CONTENT_LENGTH_BYTES + 1)

    def test_max_content_length_default_keeps_ram_scaling(self):
        # Default (absent) must remain 0 = "not explicitly set", so the
        # concurrency-scaled RAM budget in _effective_max_content_length
        # keeps working. Only an explicit value overrides it.
        policy = _payload_policy(
            {"url": "https://example.com/"},
            CrawlerSettings.from_mapping({}),
        )
        assert policy["limits"]["max_content_length_bytes"] == 0


class TestPydanticDefaultPreservesRamScaling:
    """N3: the pydantic default must not silently disable the RAM-scaled cap.

    CrawlRequest.model_dump(exclude_none=True) is what the endpoints feed the
    engine. A concrete default on CrawlPolicyLimits.max_content_length_bytes
    materializes in every dump, making the concurrency-scaled cap in
    _effective_max_content_length unreachable through /v1/crawl. The field
    must therefore default to None and only an explicit caller value flows.
    """

    def test_default_request_omits_the_cap(self):
        from app.api_models import CrawlRequest

        request = CrawlRequest(url="https://example.com/")
        payload = request.model_dump(exclude_none=True)
        dumped_limits = payload["policy"]["limits"]
        assert "max_content_length_bytes" not in dumped_limits

    def test_explicit_cap_still_flows(self):
        from app.api_models import CrawlPolicyLimits, CrawlRequest

        request = CrawlRequest(
            url="https://example.com/",
            policy={"limits": CrawlPolicyLimits(max_content_length_bytes=4096)},
        )
        policy = _payload_policy(
            request.model_dump(exclude_none=True),
            CrawlerSettings.from_mapping({}),
        )
        assert policy["limits"]["max_content_length_bytes"] == 4096


class TestMaxContentLengthBytesActuallyEnforced:
    async def test_crawl_enforces_explicit_content_cap(self, base_url, monkeypatch):
        # The advertised limit must bite end-to-end: normalization keeps it
        # and the fetcher fails the page with HTTP 413.
        from app.constants import HTTP_DECLARED_CRAWLER
        from app.crawl.engine import crawl_request
        from app.settings import CrawlerSettings

        monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")
        settings = CrawlerSettings.from_mapping({"BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS": "1"})
        result = await crawl_request(
            {
                "url": f"{base_url}/",
                "policy": {
                    "strategies": [HTTP_DECLARED_CRAWLER],
                    "limits": {"max_content_length_bytes": 64},
                },
            },
            settings=settings,
        )
        item = result["results"][0]
        assert item["status"] == "failed"
        assert item["http_status"] == 413


class TestPayloadHelpersEdges:
    """Cover remaining branches in payload_helpers.py"""

    def test_payload_policy_invalid_limits(self):
        from app.crawl.payload_helpers import _payload_policy
        from app.settings import CrawlerSettings

        with pytest.raises(ValueError) as cm:
            _payload_policy({"policy": {"limits": "not a dict"}}, CrawlerSettings.from_mapping())
        assert str(cm.value) == "invalid_policy_limits"
        with pytest.raises(ValueError) as cm:
            _payload_policy(
                {"policy": {"limits": {"max_depth": "not an int"}}}, CrawlerSettings.from_mapping()
            )
        assert str(cm.value) == "invalid_policy_limits"
