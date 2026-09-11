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
