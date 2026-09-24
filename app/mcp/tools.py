"""MCP tool registry: the four crawler primitives exposed over ``/mcp``.

Each tool delegates to the same pydantic request models and handler functions
the REST endpoints use, so the MCP surface cannot drift from the HTTP surface.
Webhook/async delivery (``async_mode`` / ``webhook_url``) is deliberately not
exposed over MCP: a tool call has no out-of-band callback channel, so those
requests are rejected with a tool execution error that names the REST path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.api.crawl_executor import execute_crawl
from app.api.map_endpoint import map_site
from app.api.render_endpoint import render as render_endpoint_handler
from app.api.response_shaping import abuild_unified_crawl_response
from app.api_models import (
    CrawlRequest,
    MapRequest,
    RenderRequest,
    SearchRequest,
    SearchResponse,
    SiteMapResponse,
    UnifiedCrawlResponse,
)
from app.crawl.strategies.browser_pool import get_browser_pool
from app.json_safe import json_default
from app.search.endpoint import search as search_endpoint_handler

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable["ToolResult"]]


@dataclass(frozen=True)
class ToolResult:
    """A ``tools/call`` outcome: text content plus optional structured data."""

    text: str
    structured: Any = None
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """A registered MCP tool definition plus its handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    handler: Handler
    title: str = ""


def _http_exception_message(error: HTTPException) -> str:
    detail = error.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail) if detail else str(error)


def _validation_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', []))}: {err.get('msg', '')}"
            for err in error.errors()
        )
    return str(error)


def _tool_error(error: HTTPException | ValueError) -> ToolResult:
    message = _http_exception_message(error) if isinstance(error, HTTPException) else str(error)
    return ToolResult(text=message, is_error=True)


def _tool_success(value: BaseModel | dict[str, Any]) -> ToolResult:
    dump = value.model_dump() if isinstance(value, BaseModel) else value
    return ToolResult(text=json.dumps(dump, default=json_default), structured=dump)


async def _handle_crawl(args: dict[str, Any], settings, shutdown_event) -> ToolResult:
    if "async_mode" in args or "webhook_url" in args:
        return ToolResult(
            text="async_mode and webhook_url are not supported over MCP; "
            "use POST /v1/crawl for webhook/async delivery.",
            is_error=True,
        )
    try:
        payload = CrawlRequest(**args)
    except (ValidationError, ValueError) as error:
        return ToolResult(text=_validation_message(error), is_error=True)
    try:
        result = await execute_crawl(
            payload.model_dump(exclude_none=True),
            settings=settings,
            shutdown_event=shutdown_event,
        )
        unified = await abuild_unified_crawl_response(
            result["results"],
            request_id=result["request_id"],
            submitted_count=result["submitted_count"],
            policy_schema_version=result["policy_schema_version"],
        )
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(unified)


async def _handle_map(args: dict[str, Any], settings, shutdown_event) -> ToolResult:
    try:
        payload = MapRequest(**args)
    except (ValidationError, ValueError) as error:
        return ToolResult(text=_validation_message(error), is_error=True)
    try:
        frontier = await map_site(payload, settings=settings, shutdown_event=shutdown_event)
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(frontier)


async def _handle_render(args: dict[str, Any], settings, shutdown_event) -> ToolResult:
    try:
        payload = RenderRequest(**args)
    except (ValidationError, ValueError) as error:
        return ToolResult(text=_validation_message(error), is_error=True)
    if get_browser_pool() is None:
        return ToolResult(
            text="Browser pool is not initialized; the service is starting up or shutting down.",
            is_error=True,
        )
    try:
        unified = await render_endpoint_handler(payload, settings=settings)
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(unified)


async def _handle_search(args: dict[str, Any], settings, shutdown_event) -> ToolResult:
    try:
        payload = SearchRequest(**args)
    except (ValidationError, ValueError) as error:
        return ToolResult(text=_validation_message(error), is_error=True)
    try:
        response = await search_endpoint_handler(payload, settings=settings)
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(response)


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


# Deterministic order: discovery primitive first, then the fetch primitives.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="search",
        title="Web search",
        description=(
            "Search the web for public URLs matching a query using the configured provider "
            "(BROWSER_CRAWLER_SEARCH_PROVIDER). Returns SERP-style results (title, url, "
            "description) and does NOT crawl result URLs. Result titles and snippets are "
            "provider-supplied and unverified; pipe result URLs into the crawl tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Web search query."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum results; clamped to the server's configured maximum.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema=_schema(SearchResponse),
        handler=_handle_search,
    ),
    ToolSpec(
        name="crawl",
        title="Crawl URLs",
        description=(
            "Execute a policy-driven crawl of one or more public HTTP(S) URLs. Accepts the same "
            "policy shape as POST /v1/crawl: strategies (ordered fallback chain of "
            "HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE, BROWSER_HEADLESS, BROWSER_HEADED_WARMED), "
            "extraction (full, trafilatura, document, dom), limits (timeout_seconds, max_retries, "
            "challenge_wait_seconds, max_discovered_links, respect_robots), discovery, scope_regex, "
            "css_selector, xpath, request_profile, browser_profile, headers, credentials "
            "(origin-scoped authorization). Webhook/async delivery is not "
            "available over MCP."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Single public HTTP(S) URL to crawl."},
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Batch of public HTTP(S) URLs to crawl. Use either url or urls.",
                },
                "policy": {
                    "type": "object",
                    "description": "CrawlPolicy per POST /v1/crawl (strategies, extraction, limits, discovery, scope_regex, css_selector, xpath, request_profile, browser_profile, headers, credentials). Omit to use service defaults.",
                },
            },
            "additionalProperties": False,
        },
        output_schema=_schema(UnifiedCrawlResponse),
        handler=_handle_crawl,
    ),
    ToolSpec(
        name="map",
        title="Map a site frontier",
        description=(
            "Discover the URL frontier reachable from a root URL: in-page discovered links plus "
            "the URLs advertised via robots.txt sitemaps and feeds. Returns the de-duplicated "
            "frontier; callers own cross-run recursion, frontier persistence, and host-policy decisions. "
            "Within a request the service already collapses URLs that normalize to the same page."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Root public HTTP(S) URL to map."},
                "policy": {
                    "type": "object",
                    "description": "Optional crawl policy for the mapping crawl; discovery is always enabled.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        output_schema=_schema(SiteMapResponse),
        handler=_handle_map,
    ),
    ToolSpec(
        name="render",
        title="Render a page with the browser",
        description=(
            "Render a URL (or batch) with the headless/headed Firefox worker and return structured "
            "output. Slower and heavier than crawl; prefer crawl for pages that do not need "
            "JavaScript rendering. Options mirror POST /v1/render: timeout_seconds, "
            "max_retries, respect_robots, include_html, include_text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Single public HTTP(S) URL to render."},
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Batch of public HTTP(S) URLs to render. Use either url or urls.",
                },
                "options": {
                    "type": "object",
                    "description": "RenderOptions per POST /v1/render (timeout_seconds, max_retries, respect_robots, include_html, include_text).",
                },
            },
            "additionalProperties": False,
        },
        output_schema=_schema(UnifiedCrawlResponse),
        handler=_handle_render,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

__all__ = ["TOOL_SPECS", "TOOLS_BY_NAME", "ToolSpec", "ToolResult"]
