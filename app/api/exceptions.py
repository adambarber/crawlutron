"""Exception handlers that normalize all errors to the ``CrawlerError`` shape.

Extracted from ``app/main.py`` so the FastAPI app factory stays thin.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Standardize on the ``CrawlerError`` shape and strip pydantic internals."""
    sanitized = [{"loc": err.get("loc"), "msg": err.get("msg")} for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Request validation failed",
            "details": {"errors": sanitized},
        },
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Unwrap FastAPI's ``{\"detail\": ...}`` envelope to ``{code, message, details}``."""
    detail = getattr(exc, "detail", None)
    headers = getattr(exc, "headers", None)
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        content = {
            "code": str(detail.get("code")),
            "message": str(detail.get("message")),
            "details": detail.get("details") or {},
        }
    else:
        content = {
            "code": f"http_{exc.status_code}",
            "message": str(detail) if detail is not None else "Request failed",
            "details": {},
        }
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


__all__ = ["validation_exception_handler", "http_exception_handler"]
