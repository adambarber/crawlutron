"""SDK-owned pydantic wire mirrors: request strictness and response tolerance."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlutron_sdk.models import (
    AdmitRequest,
    AdmitResponse,
    CrawlAccepted,
    CrawlRequest,
    CrawlResponse,
    JobControlRequest,
    SearchRequest,
    WatchState,
)


def test_crawl_request_forbids_extra_fields():
    with pytest.raises(ValidationError):
        CrawlRequest.model_validate({"url": "https://example.com/", "bogus": 1})


def test_crawl_request_defaults():
    request = CrawlRequest(url="https://example.com/")
    assert request.url == "https://example.com/"
    assert request.async_mode is False
    assert request.webhook_url is None
    assert request.policy == {}
    assert request.validators == {}


def test_crawl_request_rejects_both_url_sources():
    with pytest.raises(ValidationError):
        CrawlRequest(url="https://example.com/", urls=["https://example.com/"])


def test_crawl_request_rejects_empty_urls():
    with pytest.raises(ValidationError):
        CrawlRequest(urls=[])


def test_crawl_response_tolerates_unknown_fields():
    response = CrawlResponse.model_validate(
        {
            "request_id": "abc",
            "submitted_count": 1,
            "policy_schema_version": "1",
            "success": True,
            "results": [],
            "future_field": "whatever",
        }
    )
    assert response.request_id == "abc"
    assert response.model_extra == {"future_field": "whatever"}


def test_crawl_accepted():
    accepted = CrawlAccepted.model_validate(
        {
            "status": "accepted",
            "request_id": "abc",
            "status_url": "/v1/crawl/abc",
            "webhook_url": None,
        }
    )
    assert accepted.status == "accepted"
    assert accepted.request_id == "abc"
    assert accepted.status_url == "/v1/crawl/abc"


def test_admit_request_forbids_extra_fields():
    with pytest.raises(ValidationError):
        AdmitRequest.model_validate({"url": "https://example.com/", "extra": 1})


def test_admit_response_tolerates_unknown_fields():
    response = AdmitResponse.model_validate(
        {"admitted": True, "classification": None, "canonical_url": None, "checks": {}}
    )
    assert response.admitted is True


def test_job_control_request_action_must_be_valid():
    with pytest.raises(ValidationError):
        JobControlRequest(action="reboot")


def test_search_request_limit_bounds():
    with pytest.raises(ValidationError):
        SearchRequest(query="q", limit=0)
    with pytest.raises(ValidationError):
        SearchRequest(query="q", limit=101)
    assert SearchRequest(query="q").limit is None


def test_watch_state_tolerates_unknown_fields():
    watch = WatchState.model_validate(
        {
            "watch_id": "w1",
            "url": "https://example.com/",
            "interval_seconds": 60,
            "next_check_at": 1.0,
            "created_at": 1.0,
            "updated_at": 1.0,
            "extra_field": "ok",
        }
    )
    assert watch.model_extra == {"extra_field": "ok"}
