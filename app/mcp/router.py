"""``POST /mcp`` — MCP 2026-07-28 streamable-HTTP endpoint.

Pure router: parses the raw JSON-RPC body, validates the Origin header, and delegates
to :func:`app.mcp.server.handle_mcp_request`; responses are single JSON objects.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.api.dependencies import get_settings, get_shutdown_event
from app.mcp.constants import INVALID_REQUEST, PARSE_ERROR
from app.mcp.protocol import error_response
from app.mcp.server import handle_mcp_request, origin_allowed

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
)
async def mcp_endpoint(
    request: Request,
    settings=Depends(get_settings),
    shutdown_event=Depends(get_shutdown_event),
):
    if not origin_allowed(request.headers.get("origin")):
        return JSONResponse(
            status_code=403,
            content=error_response(None, INVALID_REQUEST, "Origin not allowed"),
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
        concurrency_tracker=getattr(request.app.state, "search_concurrency_tracker", None),
    )
    if payload is None:
        return Response(status_code=202)
    return JSONResponse(status_code=status, content=payload)


__all__ = ["router"]
