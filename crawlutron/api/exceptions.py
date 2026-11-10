"""Exception handlers that normalize all errors to the ``ErrorResponse`` shape.

Extracted from ``crawlutron/main.py`` so the FastAPI app factory stays thin.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from crawlutron.models.error_response import ErrorEnvelope
from crawlutron.models.json_value import JsonValue

logger = logging.getLogger(__name__)


def error_envelope(code: str, message: str, details: JsonValue | None = None) -> ErrorEnvelope:
    """The single definition of the wire error shape.

    ``details`` is always present — a hand-rolled site that forgets it is
    exactly the drift this factory exists to prevent.
    """
    return {"code": code, "message": message, "details": details or {}}


#: Synthetic code for an HTTPException whose detail is not envelope-shaped: a
#: declared code, not http_<status>, so details carries the numeric status.
MALFORMED_DETAIL_CODE = "malformed_error_detail"


def error_envelope_from_detail(detail: object, status_code: int) -> ErrorEnvelope:
    """The only way an ``HTTPException`` detail becomes an error envelope.

    Conforming detail (built by ``http_error``) unwraps 1:1; anything else is logged
    loudly and answered with the synthetic code plus numeric status, not a laundered message.
    """
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return error_envelope(str(detail["code"]), str(detail["message"]), detail.get("details"))
    logger.warning(
        "non_envelope_http_exception",
        extra={"status_code": status_code, "detail_type": type(detail).__name__},
    )
    return error_envelope(MALFORMED_DETAIL_CODE, "Request failed", {"status_code": status_code})


def is_quota_refusal(exc: object) -> bool:
    """Whether ``exc`` is a caller-quota refusal (429 quota_exceeded): the caller
    is at their own page/concurrency budget, so the guarded work never ran.
    """
    if not isinstance(exc, HTTPException):
        return False
    envelope = error_envelope_from_detail(getattr(exc, "detail", None), exc.status_code)
    return exc.status_code == 429 and envelope["code"] == "quota_exceeded"


def http_error(status_code: int, code: str, message: str, details: JsonValue | None = None) -> HTTPException:
    """HTTPException carrying a conforming envelope (use instead of literals)."""
    return HTTPException(status_code=status_code, detail=error_envelope(code, message, details))


def validation_http_error(error: ValueError) -> HTTPException:
    return http_error(
        422,
        "validation_error",
        "Request validation failed",
        {"errors": [{"msg": str(error)}]},
    )


BROWSER_POOL_MESSAGE = "Browser pool is not initialized; the service is starting up or shutting down."


def browser_pool_unavailable(message: str | None = None) -> HTTPException:
    """The 503 pool-down error: render endpoints and the crawl executor share
    one code + canonical message; only the pool-exhausted site overrides."""
    return http_error(503, "browser_pool_unavailable", message or BROWSER_POOL_MESSAGE)


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Standardize on the ``ErrorResponse`` shape and strip pydantic internals.

    Typed ``Exception`` for Starlette's contravariant handler contract; the isinstance
    check routes non-pydantic exceptions to the generic envelope, never the attribute access.
    """
    if not isinstance(exc, RequestValidationError):
        return JSONResponse(
            status_code=500,
            content=error_envelope("internal_error", "Unhandled exception in request handling"),
        )
    sanitized: list[JsonValue] = [
        cast(dict[str, JsonValue], {"loc": err.get("loc"), "msg": err.get("msg")}) for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_envelope("validation_error", "Request validation failed", {"errors": sanitized}),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Unwrap ``{"detail": ...}`` to ``{code, message, details}``.

    isinstance uses Starlette's base HTTPException so framework and FastAPI exceptions
    (a Starlette child) stay on the unwrap; anything else is a 500 envelope.
    """
    if not isinstance(exc, StarletteHTTPException):
        return JSONResponse(
            status_code=500,
            content=error_envelope("internal_error", "Unhandled exception in request handling"),
        )
    detail = getattr(exc, "detail", None)
    headers = getattr(exc, "headers", None)
    content = error_envelope_from_detail(detail, exc.status_code)
    if exc.status_code == 404 and not (isinstance(detail, dict) and "code" in detail):
        from crawlutron.discovery.links import DISCOVERY_LINKS

        # The metering itself lives in the anonymous-throttle middleware's
        # response stage (a route-miss is scope["route"]-less); this branch
        # only shapes the refusal surface: both discovery doors in details.
        content = error_envelope("not_found", "Request path not found", dict(DISCOVERY_LINKS))
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


__all__ = [
    "BROWSER_POOL_MESSAGE",
    "MALFORMED_DETAIL_CODE",
    "browser_pool_unavailable",
    "error_envelope",
    "error_envelope_from_detail",
    "http_error",
    "is_quota_refusal",
    "http_exception_handler",
    "validation_exception_handler",
    "validation_http_error",
]
