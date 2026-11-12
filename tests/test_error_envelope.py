"""The envelope contract: every HTTP error body is exactly {code, message, details}.
The pre-helper state had 13 literals across 7 files, three silently omitting
`details`; the factory is the single definition now, held there by the AST gate.
"""

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from crawlutron.api.exceptions import error_envelope, http_error


def _bare_request() -> Request:
    """The smallest scope a Starlette ``Request`` can be built from. The handler never
    reads its request, so tests once used a ``SimpleNamespace`` -- a stand-in for a
    type the handler is registered to receive; a real one keeps that shape on the path.
    """
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


class TestErrorEnvelope:
    def test_shape_is_code_message_details(self):
        assert error_envelope("not_found", "nope") == {"code": "not_found", "message": "nope", "details": {}}

    def test_details_default_never_none(self):
        # The handler unwrap does `detail.get("details") or {}`, but the wire contract is details-is-present;
        # a None must not survive the factory.
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
        from crawlutron.api.exceptions import http_exception_handler

        exc = http_error(503, "browser_pool_unavailable", "pool is down")
        response = asyncio.run(http_exception_handler(_bare_request(), exc))
        assert json.loads(bytes(response.body)) == {
            "code": "browser_pool_unavailable",
            "message": "pool is down",
            "details": {},
        }

    @pytest.mark.parametrize("status", [400, 404, 422, 503])
    def test_status_codes_pass_through(self, status):
        assert http_error(status, "c", "m").status_code == status

    def test_pool_down_factory_shares_canonical_message(self):
        from crawlutron.api.exceptions import BROWSER_POOL_MESSAGE, browser_pool_unavailable

        default = browser_pool_unavailable()
        assert (default.status, default.code, default.message, default.details) == (
            503,
            "browser_pool_unavailable",
            BROWSER_POOL_MESSAGE,
            None,
        )
        override = browser_pool_unavailable("exhausted: 4/4")
        # Compared whole rather than indexed into a detail dict: the DomainError carrier
        # holds the triplet directly, and asserting the full shape is this file's own job.
        assert (override.status, override.code, override.message) == (
            503,
            "browser_pool_unavailable",
            "exhausted: 4/4",
        )


class TestEnvelopeFromDetail:
    """The funnel is the only way an HTTPException detail becomes a wire body."""

    def test_conforming_detail_unwraps_one_to_one(self):
        from crawlutron.api.exceptions import error_envelope_from_detail

        exc = http_error(404, "not_found", "Unknown job_id: x")
        assert error_envelope_from_detail(exc.detail, 404) == exc.detail

    def test_non_conforming_detail_gets_the_declared_synthetic_code(self, caplog):
        """A string detail is logged loudly, not laundered into http_<status>."""
        import logging

        from crawlutron.api.exceptions import MALFORMED_DETAIL_CODE, error_envelope_from_detail

        with caplog.at_level(logging.WARNING, logger="crawlutron.api.exceptions"):
            body = error_envelope_from_detail("Not Found", 404)
        assert body == {
            "code": MALFORMED_DETAIL_CODE,
            "message": "Request failed",
            "details": {"status_code": 404},
        }
        assert any(r.message == "non_envelope_http_exception" for r in caplog.records), caplog.text
        assert not any(r.message.startswith("http_") for r in caplog.records)

    def test_none_detail_takes_the_same_path(self):
        from crawlutron.api.exceptions import MALFORMED_DETAIL_CODE, error_envelope_from_detail

        body = error_envelope_from_detail(None, 500)
        assert body["code"] == MALFORMED_DETAIL_CODE
        assert body["details"] == {"status_code": 500}

    def test_handler_keeps_wire_status_for_non_conforming_detail(self):
        """The numeric status survives; only the fabricated code dies."""
        from crawlutron.api.exceptions import MALFORMED_DETAIL_CODE, http_exception_handler

        exc = HTTPException(status_code=418, detail="I'm a teapot")
        response = asyncio.run(http_exception_handler(_bare_request(), exc))
        assert response.status_code == 418
        assert json.loads(bytes(response.body)) == {
            "code": MALFORMED_DETAIL_CODE,
            "message": "Request failed",
            "details": {"status_code": 418},
        }

    async def test_live_app_404_is_the_conforming_handler_path(self, async_client):
        """App-raised 404s unwrap 1:1 through the funnel (not the fallback): Starlette's
        route-miss 404 is the base class, outside the registered handler's MRO match, so
        the synthetic code only ever means a non-conforming construction.
        """
        resp = await async_client.get("/v1/jobs/definitely-not-a-job")
        assert resp.status_code == 404
        assert resp.json() == {
            "code": "not_found",
            "message": "Unknown job_id: definitely-not-a-job",
            "details": {},
        }

    async def test_unknown_route_404_reports_the_malformed_synthetic_code(self, async_client):
        """#668 wire-shape EVOLUTION: a route-miss surfaces as the not_found envelope metered through the
        anonymous bucket, with BOTH discovery links in details.
        """
        resp = await async_client.get("/v1/definitely-not-a-route")
        assert resp.status_code == 404
        body = resp.json()
        assert set(body) == {"code", "message", "details"}, body
        assert body["code"] == "not_found"
        assert body["details"] == {
            "capabilities": "/.well-known/crawler-service",
            "docs": "/docs",
        }

    async def test_method_not_allowed_405_is_the_envelope(self, async_client):
        """A 405 on a known route is also a framework-level starlette HTTPException and
        must surface as the envelope, not fastapi's {"detail": ...}."""
        resp = await async_client.post("/health")
        assert resp.status_code == 405
        body = resp.json()
        assert set(body) == {"code", "message", "details"}, body
        assert body["details"] == {"status_code": 405}

    async def test_unhandled_in_handler_exception_is_a_envelope_500(self):
        """A generic exception escaping a handler must yield the envelope, not Starlette's text/plain
        'Internal Server Error' -- ServerErrorMiddleware always re-raises after sending for logging. A
        throwaway app carries the REAL error surface so the shared route table is never mutated.
        """
        import httpx
        from fastapi import FastAPI

        from crawlutron.main import install_error_surface

        app = FastAPI()
        install_error_surface(app)

        @app.get("/boom")
        async def _boom():
            raise RuntimeError("kaboom")

        async with httpx.ASGITransport(app=app, raise_app_exceptions=False) as transport:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/boom")

        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json() == {
            "code": "internal_error",
            "message": "Unhandled exception in request handling",
            "details": {},
        }
