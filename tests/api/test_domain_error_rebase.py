"""The typed domain refusal re-base: every pinned refusal rides a DomainError.

Covers the raise sites, the door mappings (REST envelope, MCP tool error),
and the single-homed quota predicate the sweep classification reads.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import HTTPException

from crawlutron.config.settings import CrawlerSettings
from crawlutron.health.readiness import READINESS_STATE
from crawlutron.service.main_helpers import request_gate


def test_domain_errors_module_exposes_the_named_carriers():
    from crawlutron.service.errors import (
        AuthRefusal,
        BrowserPoolUnavailable,
        CapacityExceeded,
        CookieNamespaceRefusal,
        DomainError,
        JobNotFound,
        JobStateConflict,
        KeyVerifyThrottled,
        QuotaRefusal,
        SearchProviderError,
        SearchProviderUnavailable,
        ValidationRefusal,
        WatchLimitExceeded,
        WatchNotFound,
    )

    for cls in (
        ValidationRefusal,
        WatchNotFound,
        WatchLimitExceeded,
        CapacityExceeded,
        KeyVerifyThrottled,
        JobNotFound,
        JobStateConflict,
        BrowserPoolUnavailable,
        SearchProviderUnavailable,
        SearchProviderError,
        AuthRefusal,
        CookieNamespaceRefusal,
        QuotaRefusal,
    ):
        assert issubclass(cls, DomainError)


def test_domain_error_carries_the_wire_triplet():
    from crawlutron.service.errors import DomainError

    exc = DomainError(409, "invalid_state", "no")
    assert (exc.status, exc.code, exc.message, exc.details) == (409, "invalid_state", "no", None)


def test_service_cores_raise_domain_errors_not_http_exceptions():
    from crawlutron.service import job_control, watch_cores

    with pytest.raises(job_control.JobNotFound) as exc_info:
        job_control.raise_job_not_found("gone")
    assert (exc_info.value.status, exc_info.value.code) == (404, "not_found")
    with pytest.raises(job_control.JobStateConflict) as conflict_info:
        job_control.raise_job_conflict("bad state")
    assert (conflict_info.value.status, conflict_info.value.code) == (409, "invalid_state")
    with pytest.raises(watch_cores.WatchNotFound) as watch_info:
        raise watch_cores.WatchNotFound("gone")
    assert (watch_info.value.status, watch_info.value.code) == (404, "not_found")


async def test_request_gate_capacity_refusal_is_capacity_exceeded():
    from crawlutron.health.readiness import ReadinessState
    from crawlutron.service.errors import CapacityExceeded
    from crawlutron.service.main_helpers import readiness_state

    state = ReadinessState(max_active_requests=1)
    assert state.begin_request()
    original = readiness_state
    import crawlutron.service.main_helpers as helpers

    helpers.readiness_state = state
    try:
        with pytest.raises(CapacityExceeded) as exc_info:
            with request_gate("test_request_rejected", logger=logging.getLogger(__name__)):
                pass
    finally:
        helpers.readiness_state = original
    assert (exc_info.value.status, exc_info.value.code) == (503, "capacity_exceeded")


async def test_request_gate_value_error_maps_to_validation_envelope():
    """The default reject path keeps the byte-identical 422 envelope."""
    from crawlutron.service.errors import ValidationRefusal

    with pytest.raises(ValidationRefusal) as exc_info:
        with request_gate("test_request_rejected", logger=logging.getLogger(__name__)):
            raise ValueError("nothing crawlable here")
    assert (exc_info.value.status, exc_info.value.code) == (422, "validation_error")
    assert exc_info.value.details == {"errors": [{"msg": "nothing crawlable here"}]}
    assert READINESS_STATE.snapshot().active_requests == 0


def test_key_throttle_refusal_is_key_verify_throttled():
    from crawlutron.service.errors import KeyVerifyThrottled
    from crawlutron.service.key_throttle import KeyVerifyThrottle

    throttle = KeyVerifyThrottle()
    for _ in range(throttle.MAX_FAILURES):
        throttle.record_failure("victim-secret")
    with pytest.raises(KeyVerifyThrottled) as exc_info:
        throttle.check("victim-secret")
    assert (exc_info.value.status, exc_info.value.code) == (429, "key_verify_throttled")


async def test_an_auth_refusal_carries_the_401_triplet():
    """The 401 rides the public keys-door dependency (never a private name)."""
    from types import SimpleNamespace

    from starlette.requests import Request

    from crawlutron.service.errors import AuthRefusal
    from crawlutron_rest.api import caller_auth

    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"not-the-admin-token")],
        "app": SimpleNamespace(
            state=SimpleNamespace(crawler_settings=CrawlerSettings(key_admin_token="real"))
        ),
    }
    with pytest.raises(AuthRefusal) as exc_info:
        await caller_auth.get_keys_caller(Request(scope))
    assert (exc_info.value.status, exc_info.value.code) == (401, "invalid_api_key")


def test_the_cookie_namespace_refusal_is_its_own_code():
    """The anonymous cookie-namespace refusal is its OWN code, never the validation family."""
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.service.errors import CookieNamespaceRefusal
    from crawlutron_rest.api import caller_auth

    anon = CallerIdentity()
    with pytest.raises(CookieNamespaceRefusal) as ns:
        caller_auth.refuse_anonymous_cookie_namespace(anon, {"cookie_namespace": "ns"})
    assert ns.value.code == "anonymous_cookie_namespace"
    assert ns.value.status == 422


def test_widened_is_quota_refusal_classifies_the_quota_refusal_class():
    from crawlutron.service.errors import QuotaRefusal, is_quota_refusal

    assert is_quota_refusal(QuotaRefusal("at cap")) is True
    assert is_quota_refusal(HTTPException(status_code=429, detail="plain")) is False


def test_browser_pool_unavailable_is_a_domain_error_with_the_canonical_message():
    from crawlutron.service.errors import BrowserPoolUnavailable
    from crawlutron_rest.api.exceptions import BROWSER_POOL_MESSAGE, browser_pool_unavailable

    default = browser_pool_unavailable()
    assert isinstance(default, BrowserPoolUnavailable)
    assert default.message == BROWSER_POOL_MESSAGE
    override = browser_pool_unavailable("exhausted: 4/4")
    assert override.message == "exhausted: 4/4"


def test_search_refusals_are_domain_errors():
    from crawlutron.service.errors import (
        DomainError,
        SearchProviderError,
        SearchProviderUnavailable,
    )

    assert issubclass(SearchProviderUnavailable, DomainError)
    assert issubclass(SearchProviderError, DomainError)
    assert (SearchProviderUnavailable("p down").status, SearchProviderUnavailable("p down").code) == (
        503,
        "search_provider_unavailable",
    )
    assert (SearchProviderError("p failed").status, SearchProviderError("p failed").code) == (
        502,
        "search_provider_error",
    )


def _handler_response(handler, exc):
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    return asyncio.run(handler(request, exc))


def test_domain_error_handler_maps_to_the_error_envelope():
    from crawlutron.service.errors import DomainError
    from crawlutron_rest.api.exceptions import domain_error_handler

    response = _handler_response(domain_error_handler, DomainError(409, "invalid_state", "no"))
    assert response.status_code == 409
    import json

    assert json.loads(bytes(response.body)) == {"code": "invalid_state", "message": "no", "details": {}}


def test_domain_error_handler_details_none_becomes_empty_object():
    from crawlutron.service.errors import ValidationRefusal
    from crawlutron_rest.api.exceptions import domain_error_handler

    response = _handler_response(domain_error_handler, ValidationRefusal("c", "m", None))
    import json

    assert json.loads(bytes(response.body))["details"] == {}


def test_mcp_tool_result_catches_domain_error():
    from crawlutron.mcp.handlers import tool_result
    from crawlutron.service.errors import DomainError

    async def _raises():
        raise DomainError(404, "not_found", "Unknown job_id: x")

    result = asyncio.run(tool_result(_raises()))
    assert result.is_error is True
    assert result.text == "Unknown job_id: x"


def test_quota_refusal_across_the_sweep_predicate_covers_every_site():
    """The census's quota raise sites are check_page_quota + check_concurrency_quota."""
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.service.errors import QuotaRefusal, is_quota_refusal
    from crawlutron_rest.api import caller_auth

    pages = CallerIdentity(caller_id="k", authenticated=True, max_pages=5)
    conc = CallerIdentity(caller_id="k", authenticated=True, max_concurrency=2)
    for raise_ in (
        lambda: caller_auth.check_page_quota(pages, 10),
        lambda: caller_auth.check_concurrency_quota(conc, 2),
    ):
        with pytest.raises(QuotaRefusal) as exc_info:
            raise_()
        assert is_quota_refusal(exc_info.value) is True


def test_crawl_executor_pool_refusal_is_browser_pool_unavailable():
    from crawlutron.service.errors import BrowserPoolUnavailable

    pool_error = BrowserPoolUnavailable()
    assert pool_error.status == 503
    assert (
        pool_error.message == "Browser pool is not initialized; the service is starting up or shutting down."
    )
