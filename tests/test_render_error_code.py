"""The /v1/render 400 envelope must carry a stable error ``code``.

The handler raised ``http_error(400, str(error), str(error))``, so the
wire-visible ``code`` field was whatever the ValueError text happened to be
("batch_size_exceeded", "invalid_options",...) while every other site in the
service emits the stable ``"validation_error"``. Clients key error handling on
``code``; a free-text code is an untracked, unenumerable contract.
"""

import pytest
from fastapi import HTTPException

from app.api.exceptions import error_envelope_from_detail
from app.api.render_endpoint import render
from app.api_models import RenderRequest
from app.settings import CrawlerSettings
from tests._browser_doubles import FakeBrowserPool, owned_browser_pool


@pytest.fixture()
def _pool_up():
    """The pool-down guard answers 503 before validation runs; own the premise
    so the test exercises the ValueError branch, not startup ordering."""
    with owned_browser_pool(FakeBrowserPool()):
        yield


async def test_render_value_error_reports_validation_error_code(monkeypatch, _pool_up):
    from dataclasses import replace

    from app.health.readiness import READINESS_STATE

    settings = replace(CrawlerSettings.from_mapping(), max_batch_size=1)
    active_before = READINESS_STATE.snapshot().active_requests

    payload = RenderRequest(urls=["http://example.com/", "http://example.com/2"])
    with pytest.raises(HTTPException) as exc_info:
        await render(payload, settings)

    # Through the accessor the service itself uses. ``HTTPException.detail`` is
    # declared ``str | None`` and carries an envelope dict here, so a direct
    # index was reaching past what the type says is there; this is also the
    # shape a client actually receives.
    detail = error_envelope_from_detail(exc_info.value.detail, exc_info.value.status_code)
    assert detail["code"] == "validation_error", detail
    # The ValueError text stays as the human-readable message, not the code.
    assert detail["message"] == "batch_size_exceeded"
    # The refusal hands its readiness slot back: the gate that converts the
    # rejection also releases it, so /v1/render cannot hold a slot per bad
    # request. /v1/crawl's equivalent is tests/api/test_request_gate.py.
    assert READINESS_STATE.snapshot().active_requests == active_before
