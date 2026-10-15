"""Exception handlers that normalize all errors to the ``ErrorResponse`` shape.

Extracted from ``app/main.py`` so the FastAPI app factory stays thin.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.models.error_response import ErrorEnvelope
from app.models.json_value import JsonValue

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


def error_envelope_from_detail(detail: Any, status_code: int) -> ErrorEnvelope:
    """The only way an ``HTTPException`` detail becomes an error envelope.

    Conforming detail (built by :func:`http_error`) unwraps 1:1. Anything else
    means someone raised ``HTTPException`` outside the helpers -- a string
    like Starlette's own 404/405, or a dict missing ``code``/``message`` --
    so it is logged loudly (the drift must be visible in the server log, not
    laundered into a fabricated ``http_<status>`` code whose message is a
    ``str()`` of arbitrary content) and answered with the declared synthetic
    code plus the numeric status in ``details``.
    """
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        return error_envelope(str(detail["code"]), str(detail["message"]), detail.get("details"))
    logger.warning(
        "non_envelope_http_exception",
        extra={"status_code": status_code, "detail_type": type(detail).__name__},
    )
    return error_envelope(MALFORMED_DETAIL_CODE, "Request failed", {"status_code": status_code})


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

    The parameter is typed ``Exception`` (Starlette's handler contract is
    contravariant: a handler registered for a base type must accept any
    subtype) rather than ``RequestValidationError``; the isinstance check
    below keeps the pydantic-specific branch honest and routes anything else
    to the generic envelope so an unexpected exception type cannot reach the
    attribute access and 500.
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
    """Unwrap FastAPI's ``{"detail": ...}`` envelope to ``{code, message, details}``.

    ``exc`` is ``Exception`` to satisfy Starlette's contravariant handler
    registration; the isinstance check below preserves the FastAPI-specific
    unwrap, and anything else degrades to a generic 500 instead of an
    AttributeError on a handler that was registered to catch it.
    """
    if not isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=500,
            content=error_envelope("internal_error", "Unhandled exception in request handling"),
        )
    detail = getattr(exc, "detail", None)
    headers = getattr(exc, "headers", None)
    content = error_envelope_from_detail(detail, exc.status_code)
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


__all__ = [
    "BROWSER_POOL_MESSAGE",
    "MALFORMED_DETAIL_CODE",
    "browser_pool_unavailable",
    "error_envelope",
    "error_envelope_from_detail",
    "http_error",
    "http_exception_handler",
    "validation_exception_handler",
    "validation_http_error",
]
