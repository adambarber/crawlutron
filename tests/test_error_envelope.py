"""#1722 envelope contract: every HTTP error body is exactly {code, message, details}.

The pre-helper state had 13 hand-rolled dict literals across 7 files; three
silently omitted ``details``, and any future site could drift the key names
freedom-planting the drift-masking fallback in http_exception_handler. The
factory is now the single definition; the AST drift detector in
test_safety_invariants.py keeps it that way.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.exceptions import error_envelope, http_error


class TestErrorEnvelope:
    def test_shape_is_code_message_details(self):
        assert error_envelope("not_found", "nope") == {"code": "not_found", "message": "nope", "details": {}}

    def test_details_default_never_none(self):
        # The handler unwrap does `detail.get("details") or {}`, but the wire
        # contract is details-is-present; a None must not survive the factory.
        envelope = error_envelope("validation_error", "bad", None)
        assert envelope["details"] == {}

    def test_details_content_preserved(self):
        envelope = error_envelope("pending", "still running", {"status": "RUNNING"})
        assert envelope["details"] == {"status": "RUNNING"}

    def test_http_error_carries_envelope_detail(self):
        exc = http_error(422, "validation_error", "bad input", {"errors": [{"msg": "x"}]})
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 422
        assert exc.detail == {
            "code": "validation_error",
            "message": "bad input",
            "details": {"errors": [{"msg": "x"}]},
        }

    def test_handler_unwraps_to_same_wire_shape(self):
        from app.api.exceptions import http_exception_handler

        exc = http_error(503, "browser_pool_unavailable", "pool is down")
        response = asyncio.run(http_exception_handler(SimpleNamespace(), exc))
        assert json.loads(response.body) == {
            "code": "browser_pool_unavailable",
            "message": "pool is down",
            "details": {},
        }

    @pytest.mark.parametrize("status", [400, 404, 422, 503])
    def test_status_codes_pass_through(self, status):
        assert http_error(status, "c", "m").status_code == status

    def test_pool_down_factory_shares_canonical_message(self):
        from app.api.exceptions import BROWSER_POOL_MESSAGE, browser_pool_unavailable

        default = browser_pool_unavailable()
        assert default.detail == {
            "code": "browser_pool_unavailable",
            "message": BROWSER_POOL_MESSAGE,
            "details": {},
        }
        override = browser_pool_unavailable("exhausted: 4/4")
        assert override.detail["message"] == "exhausted: 4/4"


class TestEnvelopeFromDetail:
    """M8: the funnel is the only way an HTTPException detail becomes a wire body."""

    def test_conforming_detail_unwraps_one_to_one(self):
        from app.api.exceptions import error_envelope_from_detail

        exc = http_error(404, "not_found", "Unknown job_id: x")
        assert error_envelope_from_detail(exc.detail, 404) == exc.detail

    def test_non_conforming_detail_gets_the_declared_synthetic_code(self, caplog):
        """A string detail is logged loudly, not laundered into http_<status>."""
        import logging

        from app.api.exceptions import MALFORMED_DETAIL_CODE, error_envelope_from_detail

        with caplog.at_level(logging.WARNING, logger="app.api.exceptions"):
            body = error_envelope_from_detail("Not Found", 404)
        assert body == {
            "code": MALFORMED_DETAIL_CODE,
            "message": "Request failed",
            "details": {"status_code": 404},
        }
        assert any(r.message == "non_envelope_http_exception" for r in caplog.records), caplog.text
        assert not any(r.message.startswith("http_") for r in caplog.records)

    def test_none_detail_takes_the_same_path(self):
        from app.api.exceptions import MALFORMED_DETAIL_CODE, error_envelope_from_detail

        body = error_envelope_from_detail(None, 500)
        assert body["code"] == MALFORMED_DETAIL_CODE
        assert body["details"] == {"status_code": 500}

    def test_handler_keeps_wire_status_for_non_conforming_detail(self):
        """The numeric status survives; only the fabricated code dies."""
        from app.api.exceptions import MALFORMED_DETAIL_CODE, http_exception_handler

        exc = HTTPException(status_code=418, detail="I'm a teapot")
        response = asyncio.run(http_exception_handler(SimpleNamespace(), exc))
        assert response.status_code == 418
        assert json.loads(response.body) == {
            "code": MALFORMED_DETAIL_CODE,
            "message": "Request failed",
            "details": {"status_code": 418},
        }

    async def test_live_app_404_is_the_conforming_handler_path(self, async_client):
        """App-raised 404s unwrap 1:1 through the funnel (not the fallback).

        Also pins which 404s reach the handler at all: Starlette's own
        route-miss 404 never reaches this handler (its exception is the
        base class, outside the registered handler's MRO match), so the
        synthetic code only ever means a non-conforming construction.
        """
        resp = await async_client.get("/v1/jobs/definitely-not-a-job")
        assert resp.status_code == 404
        assert resp.json() == {
            "code": "not_found",
            "message": "Unknown job_id: definitely-not-a-job",
            "details": {},
        }
