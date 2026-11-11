"""The /v1/render 400 envelope must carry a stable error ``code``: it used to echo
whatever the ValueError text was while every other site emits ``"validation_error"``,
and clients key error handling on ``code`` -- a free-text code is unenumerable.
"""

import pytest

from crawlutron.api.caller_auth import Tracking
from crawlutron.api.identity_router import render as render_door
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.render_request import RenderRequest
from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION
from crawlutron.service.errors import ValidationRefusal
from tests._browser_doubles import FakeBrowserPool, owned_browser_pool


@pytest.fixture()
def _pool_up():
    """The pool-down guard answers 503 before validation runs; own the premise
    so the test exercises the ValueError branch, not startup ordering."""
    with owned_browser_pool(FakeBrowserPool()):
        yield


async def test_render_value_error_reports_validation_error_code(monkeypatch, _pool_up):
    from dataclasses import replace

    from crawlutron.health.readiness import READINESS_STATE

    settings = replace(CrawlerSettings.from_mapping(), max_batch_size=1)
    active_before = READINESS_STATE.snapshot().active_requests

    payload = RenderRequest(urls=["http://example.com/", "http://example.com/2"])
    with pytest.raises(ValidationRefusal) as exc_info:
        await render_door(payload, CallerIdentity(), Tracking(), CALLER_QUOTA_ADMISSION, settings)

    # The carrier IS the envelope triplet the service raises (the render door pins
    # the 400 status through its own rejection factory at the gate).
    assert exc_info.value.code == "validation_error", exc_info.value
    # The ValueError text stays as the human-readable message, not the code.
    assert exc_info.value.message == "batch_size_exceeded"
    # The refusal hands its readiness slot back: the gate that converts the rejection also releases it, so
    # /v1/render cannot hold a slot per bad request. /v1/crawl's equivalent is tests/api/test_request_gate.py.
    assert READINESS_STATE.snapshot().active_requests == active_before
