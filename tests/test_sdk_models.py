"""SDK-owned pydantic wire mirrors: request strictness and response tolerance."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

import crawlutron_sdk.models as sdk_models
from crawlutron.models.search_request import SearchRequest as ServerSearchRequest
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
from tests._validation_helpers import assert_non_finite_float_field_refused


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


def test_search_request_limit_accepts_full_server_range():
    """The SDK must not second-guess the server's clamp: the full range admits."""
    with pytest.raises(ValidationError):
        SearchRequest(query="q", limit=0)
    assert SearchRequest(query="q", limit=100).limit == 100
    assert SearchRequest(query="q").limit is None
    description = SearchRequest.model_fields["limit"].description
    assert description is not None and "clamps" in description


def _upper_bound(model: type[BaseModel]) -> int | None:
    for constraint in model.model_fields["limit"].metadata:
        le = getattr(constraint, "le", None)
        if le is not None:
            return le
    return None


def test_search_request_limit_bound_tracks_the_server_model():
    """Drift guard: the SDK's upper bound must cover the server model's bound."""
    server_le = _upper_bound(ServerSearchRequest)
    sdk_le = _upper_bound(SearchRequest)
    assert server_le is not None and sdk_le is not None
    assert sdk_le >= server_le


def _sdk_request_models() -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name, obj in vars(sdk_models).items()
        if isinstance(obj, type)
        and issubclass(obj, sdk_models.BaseModel)
        and name.endswith("Request")
        and obj.__module__ == sdk_models.__name__
    }


def test_sdk_docstring_extra_rule_names_every_non_forbid_request_model():
    """Drift detector: the docstring states the request models' extra= rule.

    Every request model deviating from forbid must be named there."""
    doc = sdk_models.__doc__ or ""
    unnamed = sorted(
        name
        for name, model in _sdk_request_models().items()
        if model.model_config.get("extra") != "forbid" and name not in doc
    )
    assert not unnamed, f"docstring's extra= rule omits non-forbid request models: {unnamed}"
    assert _sdk_request_models(), "detector found no request models; the premise is void"


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


def test_sdk_crawl_request_accepts_delta_knobs():
    request = CrawlRequest.model_validate(
        {
            "url": "https://example.com/",
            "changed_since": 1730000000.0,
            "baseline_request_id": "req_abc123",
        }
    )
    dumped = request.model_dump()
    assert dumped["changed_since"] == 1730000000.0
    assert dumped["baseline_request_id"] == "req_abc123"


def test_sdk_crawl_request_delta_knobs_default_to_none():
    request = CrawlRequest.model_validate({"url": "https://example.com/"})
    dumped = request.model_dump()
    assert dumped["changed_since"] is None
    assert dumped["baseline_request_id"] is None


def test_sdk_crawl_response_tolerates_delta_fields():
    response = CrawlResponse(
        request_id="abc",
        submitted_count=1,
        policy_schema_version="1",
        success=True,
        delta_summary={"changed": 1},
        results=[
            {
                "url": "https://example.com/",
                "delta_classification": "changed",
                "delta_evidence": {"etag": "W/1"},
            }
        ],
    )
    assert response.delta_summary == {"changed": 1}
    assert response.results[0]["delta_classification"] == "changed"
    assert response.results[0]["delta_evidence"] == {"etag": "W/1"}


def test_sdk_crawl_request_rejects_non_finite_changed_since():
    assert assert_non_finite_float_field_refused(
        lambda **kw: CrawlRequest.model_validate({"url": "https://example.com/", **kw}),
        "changed_since",
    )
