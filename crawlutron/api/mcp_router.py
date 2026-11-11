"""``POST /mcp`` — MCP 2026-07-28 streamable-HTTP endpoint.

Pure HTTP-wire concern, so it lives in api: parses the JSON-RPC body and
delegates to :func:`crawlutron.mcp.server.handle_mcp_request`.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from crawlutron.api.caller_auth import get_caller_identity
from crawlutron.api.dependencies import get_concurrency_tracker, get_settings, get_shutdown_event
from crawlutron.mcp.constants import INVALID_API_KEY, INVALID_REQUEST, PARSE_ERROR
from crawlutron.mcp.handlers import http_exception_message
from crawlutron.mcp.protocol import error_response
from crawlutron.mcp.server import handle_mcp_request
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.error_response import ErrorResponse
from crawlutron.service.errors import DomainError

router = APIRouter(tags=["MCP"])


@router.post(
    "/mcp",
    response_model=None,
    summary="MCP 2026-07-28 streamable HTTP endpoint",
    description=(
        "Stateless Model Context Protocol endpoint (2026-07-28). Every request is an "
        "independent JSON-RPC POST carrying its protocol version and client capabilities "
        "in _meta; there is no initialize handshake and no session. Call server/discover "
        "to learn supported versions, tools/list for the tool set, and tools/call to invoke "
        "search, crawl, map, or render. Requests must include MCP-Protocol-Version, "
        "Mcp-Method, and (for tools/call) Mcp-Name headers matching the body."
    ),
    responses={
        202: {"description": "Accepted for delivery; the result arrives on the webhook."},
        400: {"model": ErrorResponse, "description": "The body is not a JSON-RPC request."},
        401: {"model": ErrorResponse, "description": "Unknown or revoked API key."},
    },
)
async def mcp_endpoint(
    request: Request,
    settings=Depends(get_settings),
    shutdown_event=Depends(get_shutdown_event),
):
    # The disallowed-Origin gate is the middleware's (one gate, per-surface shape).
    # An invalid credential must be a 401, never a benign 200 (no key stays anonymous).
    try:
        caller: CallerIdentity = await get_caller_identity(request)
    except (HTTPException, DomainError) as error:
        message = str(error.message) if isinstance(error, DomainError) else http_exception_message(error)
        return JSONResponse(
            status_code=401,
            content=error_response(None, INVALID_API_KEY, message),
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content=error_response(None, PARSE_ERROR, "Parse error"),
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content=error_response(None, INVALID_REQUEST, "Invalid Request"),
        )
    status, payload = await handle_mcp_request(
        body,
        protocol_version_header=request.headers.get("mcp-protocol-version"),
        method_header=request.headers.get("mcp-method"),
        name_header=request.headers.get("mcp-name"),
        settings=settings,
        shutdown_event=shutdown_event,
        concurrency_tracker=get_concurrency_tracker(request),
        job_store=getattr(request.app.state, "job_store", None),
        watch_store=getattr(request.app.state, "watch_store", None),
        result_store=getattr(request.app.state, "result_store", None),
        caller=caller,
    )
    if payload is None:
        return Response(status_code=202)
    return JSONResponse(status_code=status, content=payload)


__all__ = ["router"]
