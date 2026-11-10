"""Delta fields on UnifiedCrawlResult and UnifiedCrawlResponse are nullable and additive."""

from __future__ import annotations

from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult


def test_unified_crawl_result_delta_fields_default_to_none():
    result = UnifiedCrawlResult(
        original_url="https://example.com/",
        status="success",
        classification="rendered",
    )
    assert result.delta_classification is None
    assert result.delta_evidence is None


def test_unified_crawl_result_delta_fields_round_trip():
    result = UnifiedCrawlResult(
        original_url="https://example.com/",
        status="success",
        classification="rendered",
        delta_classification="changed",
        delta_evidence={"etag": "W/1", "changed": True},
    )
    assert result.delta_classification == "changed"
    assert result.delta_evidence == {"etag": "W/1", "changed": True}
    dumped = result.model_dump()
    assert dumped["delta_classification"] == "changed"
    assert dumped["delta_evidence"] == {"etag": "W/1", "changed": True}


def test_unified_crawl_response_delta_summary_defaults_to_none():
    response = UnifiedCrawlResponse(
        request_id="req_1",
        submitted_count=1,
        policy_schema_version="1",
        success=True,
        results=[],
    )
    assert response.delta_summary is None


def test_unified_crawl_response_delta_summary_round_trip():
    response = UnifiedCrawlResponse(
        request_id="req_1",
        submitted_count=1,
        policy_schema_version="1",
        success=True,
        results=[],
        delta_summary={"changed": 1, "unchanged": 0},
    )
    assert response.delta_summary == {"changed": 1, "unchanged": 0}
    dumped = response.model_dump()
    assert dumped["delta_summary"] == {"changed": 1, "unchanged": 0}
