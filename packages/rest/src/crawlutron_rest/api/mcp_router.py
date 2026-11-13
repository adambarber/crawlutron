"""``POST /mcp`` — MCP 2026-07-28 streamable-HTTP endpoint.

Pure HTTP-wire concern, so it lives in api: parses the JSON-RPC body and
delegates to :func:`crawlutron_mcp.server.handle_mcp_request`.
"""

from __future__ import annotations

import json

from crawlutron_mcp.constants import INVALID_API_KEY, INVALID_REQUEST, PARSE_ERROR, RATE_LIMITED
from crawlutron_mcp.protocol import error_response
from crawlutron_mcp.server import handle_mcp_request
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.error_response import ErrorResponse
from crawlutron.service.errors import DomainError, KeyVerifyThrottled
from crawlutron_rest.api.caller_auth import get_caller_identity
from crawlutron_rest.api.dependencies import get_concurrency_tracker, get_settings, get_shutdown_event

router = APIRouter(tags=["MCP"])


def _auth_denial_message(error: HTTPException | DomainError) -> str:
    """Render a caller-auth refusal: a DomainError carries ``message``; an
    HTTPException's conforming detail (``dict`` with ``message``) unwraps to it."""
    if isinstance(error, DomainError):
        return str(error.message)
    detail = error.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail) if detail else str(error)


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
    # A key-verify THROTTLE is a 429 (#929), never the 401 an invalid key gets.
    try:
        caller: CallerIdentity = await get_caller_identity(request)
    except KeyVerifyThrottled as throttled:
        return JSONResponse(
            status_code=429,
            content=error_response(None, RATE_LIMITED, str(throttled.message)),
        )
    except (HTTPException, DomainError) as error:
        return JSONResponse(
            status_code=401,
            content=error_response(None, INVALID_API_KEY, _auth_denial_message(error)),
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
    # An MCP door built without the stash is a wiring defect: refuse 500
    # caller_quota_unwired instead of admitting through the silent singleton default.
    caller_auth = getattr(request.app.state, "caller_quota_admission", None)
    if caller_auth is None:
        raise DomainError(
            500,
            "caller_quota_unwired",
            "the caller-quota admission port is not wired into app.state",
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
        caller_auth=caller_auth,
    )
    if payload is None:
        return Response(status_code=202)
    return JSONResponse(status_code=status, content=payload)


__all__ = ["router"]
