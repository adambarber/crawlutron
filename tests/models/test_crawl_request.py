"""Delta crawl knobs on the core CrawlRequest are optional and default to None."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlutron.models.crawl_request import CrawlRequest
from tests._validation_helpers import assert_non_finite_float_field_refused


def test_crawl_request_accepts_delta_knobs():
    request = CrawlRequest(
        url="https://example.com/",
        changed_since=1730000000.0,
        baseline_request_id="req_abc123",
    )
    assert request.changed_since == 1730000000.0
    assert request.baseline_request_id == "req_abc123"


def test_crawl_request_delta_knobs_default_to_none():
    request = CrawlRequest(url="https://example.com/")
    assert request.changed_since is None
    assert request.baseline_request_id is None


def test_crawl_request_still_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        CrawlRequest.model_validate({"url": "https://example.com/", "not_a_field": 1})


def test_crawl_request_url_source_rule_still_applies():
    with pytest.raises(ValidationError):
        CrawlRequest(url="https://example.com/", urls=["https://example.com/"])


def test_crawl_request_rejects_non_finite_changed_since():
    assert assert_non_finite_float_field_refused(
        lambda **kw: CrawlRequest(url="https://example.com/", **kw), "changed_since"
    )
