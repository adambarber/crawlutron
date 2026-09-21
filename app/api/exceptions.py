"""Exception handlers that normalize all errors to the ``CrawlerError`` shape.

Extracted from ``app/main.py`` so the FastAPI app factory stays thin.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def error_envelope(code: str, message: str, details: dict | None = None) -> dict:
    """The single definition of the wire error shape (#1722).

    ``details`` is always present — a hand-rolled site that forgets it is
    exactly the drift this factory exists to prevent.
    """
    return {"code": code, "message": message, "details": details or {}}


def http_error(status_code: int, code: str, message: str, details: dict | None = None) -> HTTPException:
    """HTTPException carrying a conforming envelope (use instead of literals)."""
    return HTTPException(status_code=status_code, detail=error_envelope(code, message, details))


BROWSER_POOL_MESSAGE = "Browser pool is not initialized; the service is starting up or shutting down."


def browser_pool_unavailable(message: str | None = None) -> HTTPException:
    """The 503 pool-down error: render endpoints and the crawl executor share
    one code + canonical message; only the pool-exhausted site overrides."""
    return http_error(503, "browser_pool_unavailable", message or BROWSER_POOL_MESSAGE)


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Standardize on the ``CrawlerError`` shape and strip pydantic internals.

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
    sanitized = [{"loc": err.get("loc"), "msg": err.get("msg")} for err in exc.errors()]
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
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        content = error_envelope(str(detail.get("code")), str(detail.get("message")), detail.get("details"))
    else:
        content = error_envelope(
            f"http_{exc.status_code}", str(detail) if detail is not None else "Request failed"
        )
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


__all__ = [
    "BROWSER_POOL_MESSAGE",
    "browser_pool_unavailable",
    "error_envelope",
    "http_error",
    "http_exception_handler",
    "validation_exception_handler",
]
