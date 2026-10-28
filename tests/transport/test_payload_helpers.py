"""Payload helpers: policy construction, advertised limits and invalid-limit handling."""

import pytest
from pydantic import ValidationError

from app.admission.payload_helpers import payload_policy
from app.admission.policy import PolicyTier, validate_wire_policy
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy


def _normalized_policy(**policy: object) -> CrawlPolicy:
    """The wire policy a caller sends, resolved at the admission edge.

    Every test here is about one policy field, so the wire mapping is written
    once; rejections still raise from the real entry point.
    """
    return validate_wire_policy(dict(policy), PolicyTier.model_defaults())


class TestPayloadPolicyCarriesAdvertisedLimits:
    def test_max_rps_survives_normalization(self):
        policy = _normalized_policy(limits={"max_rps": 2.5})
        assert policy.limits.max_rps == 2.5

    def test_max_rps_default_is_zero(self):
        policy = _normalized_policy()
        assert policy.limits.max_rps == 0.0

    def test_max_rps_out_of_range_rejected(self):
        # CrawlPolicyLimits bounds max_rps to [0, 100]; out-of-range requests must be rejected, not silently
        # clamped into a different contract.
        with pytest.raises(ValueError):
            _normalized_policy(limits={"max_rps": 500})

    def test_max_content_length_bytes_survives_normalization(self):
        policy = _normalized_policy(limits={"max_content_length_bytes": 1024})
        assert policy.limits.max_content_length_bytes == 1024

    def test_max_content_length_above_ceiling_rejected(self):
        # An explicit cap must not be a caller-controlled RAM/decompression override: over the hard ceiling it
        # is rejected, like max_rps, never silently clamped into a different contract.
        from app.vocabulary.limits import MAX_CONTENT_LENGTH_BYTES

        with pytest.raises(ValueError):
            _normalized_policy(limits={"max_content_length_bytes": MAX_CONTENT_LENGTH_BYTES + 1})

    def test_max_content_length_at_ceiling_survives(self):
        from app.vocabulary.limits import MAX_CONTENT_LENGTH_BYTES

        policy = _normalized_policy(limits={"max_content_length_bytes": MAX_CONTENT_LENGTH_BYTES})
        assert policy.limits.max_content_length_bytes == MAX_CONTENT_LENGTH_BYTES

    def test_max_content_length_above_ceiling_is_a_validation_error(self):
        # The pydantic boundary must 422 oversized caps (le= on the model).

        from app.models.crawl_policy_limits import CrawlPolicyLimits
        from app.vocabulary.limits import MAX_CONTENT_LENGTH_BYTES

        with pytest.raises(ValidationError):
            CrawlPolicyLimits(max_content_length_bytes=MAX_CONTENT_LENGTH_BYTES + 1)

    def test_max_content_length_default_keeps_ram_scaling(self):
        # Default (absent) must remain 0 = "not explicitly set", so the concurrency-scaled RAM budget in
        # effective_max_content_length keeps working. Only an explicit value overrides it.
        policy = _normalized_policy()
        assert policy.limits.max_content_length_bytes == 0


class TestPydanticDefaultPreservesRamScaling:
    """The pydantic default must not silently disable the RAM-scaled cap.

    A concrete default materializes in every ``model_dump``, making the
    concurrency-scaled cap unreachable through /v1/crawl.
    """

    def test_default_request_omits_the_cap(self):
        from app.models.crawl_request import CrawlRequest

        request = CrawlRequest(url="https://example.com/")
        payload = request.model_dump(exclude_none=True)
        dumped_limits = payload["policy"]["limits"]
        assert "max_content_length_bytes" not in dumped_limits

    def test_explicit_cap_still_flows(self):
        from app.models.crawl_policy_limits import CrawlPolicyLimits
        from app.models.crawl_request import CrawlRequest

        request = CrawlRequest(
            url="https://example.com/",
            policy={"limits": CrawlPolicyLimits(max_content_length_bytes=4096)},
        )
        policy = payload_policy(request, PolicyTier.model_defaults())
        assert policy.limits.max_content_length_bytes == 4096


class TestMaxContentLengthBytesActuallyEnforced:
    async def test_crawl_enforces_explicit_content_cap(self, base_url, monkeypatch):
        # The advertised limit must bite end-to-end: normalization keeps it and the fetcher fails the page
        # with HTTP 413.
        from app.orchestration.engine import crawl_request
        from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER

        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_ALLOW_PRIVATE_TARGETS": "1"})
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
        assert item.status == "failed"
        assert item.http_status == 413


class TestPayloadHelpersEdges:
    """Cover remaining branches in payload_helpers.py"""

    def test_payload_policy_invalid_limits(self):
        from app.admission.policy import validate_wire_policy

        with pytest.raises(ValueError) as cm:
            validate_wire_policy({"limits": "not a dict"}, PolicyTier.model_defaults())
        assert str(cm.value) == "invalid_policy_limits"
        with pytest.raises(ValueError) as cm:
            validate_wire_policy({"limits": {"max_depth": "not an int"}}, PolicyTier.model_defaults())
        assert str(cm.value) == "invalid_policy_limits"


class TestExplicitEmptyStrategiesIsRejectedNotDefaulted:
    """An absent list means "use the default chain"; an empty one is a mistake.

    ``policy.get(...) or DEFAULT_STRATEGIES`` treats ``[]`` as falsy, so an
    explicitly empty chain was replaced by a default that included a browser.
    """

    def test_explicitly_empty_strategies_is_rejected(self):
        with pytest.raises(ValueError, match="invalid_policy_strategies"):
            _normalized_policy(strategies=[])

    def test_an_absent_strategies_key_still_gets_the_default_chain(self):
        """Rejection must not cost the documented default."""
        from app.vocabulary.strategies import DEFAULT_STRATEGIES

        assert [s.value for s in _normalized_policy().strategies] == DEFAULT_STRATEGIES

    def test_a_non_list_strategies_is_still_rejected(self):
        with pytest.raises(ValueError, match="invalid_policy_strategies"):
            _normalized_policy(strategies="http")

    def test_an_unsupported_strategy_is_still_rejected(self):
        with pytest.raises(ValueError, match="invalid_policy_strategy"):
            _normalized_policy(strategies=["telepathy"])
