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
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.api.crawl_executor import execute_crawl
from app.api.map_endpoint import map_site
from app.api.render_endpoint import render as render_endpoint_handler
from app.api.response_shaping import aunified_response_from_result
from app.json_safe import json_default
from app.models.crawl_and_extract_response import CrawlAndExtractResponse
from app.models.crawl_request import CrawlRequest
from app.models.extracted_page import ExtractedPage
from app.models.map_request import MapRequest
from app.models.render_request import RenderRequest
from app.models.search_request import SearchRequest
from app.models.search_response import SearchResponse
from app.models.site_map_response import SiteMapResponse
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.search.endpoint import search as search_endpoint_handler
from app.transport.browser_pool import get_browser_pool

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable["ToolResult"]]

#: The request fields the crawl tool advertises *and* rejects from one list, so
#: the schema cannot offer a field the handler then refuses.
_UNSUPPORTED_CRAWL_FIELDS = frozenset({"async_mode", "webhook_url"})


@dataclass(frozen=True)
class ToolResult:
    """A ``tools/call`` outcome: text content plus optional structured data."""

    text: str
    structured: Any = None
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """A registered MCP tool definition plus its handler.

    ``request_model`` is the source of truth for both the advertised
    ``inputSchema`` and the validation of a call's arguments, so the two cannot
    describe different contracts. That used to be a hand-written JSON schema
    beside a handler that validated independently: the schema advertised a
    narrower contract than the handler accepted while claiming
    ``additionalProperties: False``, which pydantic does not enforce -- a
    client reading the schema could neither use a real field nor rely on
    extras being rejected. A field added to a request model is now advertised
    the day it is added.

    ``unsupported_fields`` names the model fields this tool deliberately will
    not accept. It is the same list the handler rejects on, so the advertised
    contract and the enforced one cannot drift apart either.
    """

    name: str
    description: str
    request_model: type[BaseModel]
    output_schema: dict[str, Any] | None
    handler: Handler
    title: str = ""
    unsupported_fields: Collection[str] = ()

    @property
    def input_schema(self) -> dict[str, Any]:
        """The request model's own JSON schema, less the unsupported fields."""
        schema = self.request_model.model_json_schema()
        properties = {
            k: v for k, v in schema.get("properties", {}).items() if k not in self.unsupported_fields
        }
        required = [k for k in schema.get("required", []) if k not in self.unsupported_fields]
        return {
            **{k: v for k, v in schema.items() if k not in ("properties", "required")},
            "properties": properties,
            "required": required,
        }


def _http_exception_message(error: HTTPException) -> str:
    detail = error.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail) if detail else str(error)


def validation_message(error: Exception) -> str:
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


async def _tool_result(coro) -> ToolResult:
    """Cross the MCP boundary for one tool call: run it, or report it as an error.

    Every handler does the same two things -- await the endpoint, and turn an
    ``HTTPException``/``ValueError`` from it into a tool error rather than a
    raised traceback. Only the endpoint differs.

    The envelope lives here because it is the boundary's contract: a handler
    that spelled its own ``except`` could forget the arm, and the failure mode
    is an exception escaping a JSON-RPC call as a 500.
    """
    try:
        result = await coro
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(result)


async def _crawled(payload: CrawlRequest, settings, shutdown_event):
    """Run the crawl endpoint and shape it, so the handler owns only the boundary."""
    return await aunified_response_from_result(
        await execute_crawl(
            payload.model_dump(exclude_none=True),
            settings=settings,
            shutdown_event=shutdown_event,
        )
    )


def _delivery_refusal(payload: CrawlRequest) -> ToolResult | None:
    """The one refusal both crawl tools make: delivery is a REST concern.

    Shared so the message cannot differ between the two tools an agent chooses
    between, and so a field added to that set is refused by both.
    """
    if _UNSUPPORTED_CRAWL_FIELDS & payload.model_fields_set:
        return ToolResult(
            text="async_mode and webhook_url are not supported over MCP; "
            "use POST /v1/crawl for webhook/async delivery.",
            is_error=True,
        )
    return None


async def _handle_crawl(payload: CrawlRequest, settings, shutdown_event) -> ToolResult:
    refusal = _delivery_refusal(payload)
    if refusal is not None:
        return refusal
    return await _tool_result(_crawled(payload, settings, shutdown_event))


async def _extracted(response: UnifiedCrawlResponse) -> CrawlAndExtractResponse:
    """The crawl response read as extraction records.

    The same projection shape over the same stored data, with the provenance
    dropped: an MCP result carries its payload twice, so per-page bulk is
    doubled on the wire, and a caller who only wants content pays for attempts,
    headers, diagnostics and evidence anyway.
    """
    return CrawlAndExtractResponse(
        submitted_count=response.submitted_count,
        pages=[
            ExtractedPage(
                url=result.normalized_url,
                status=result.status,
                classification=result.classification,
                title=result.metadata.title,
                markdown=result.markdown,
            )
            for result in response.results
        ],
    )


async def _handle_crawl_and_extract(payload: CrawlRequest, settings, shutdown_event) -> ToolResult:
    refusal = _delivery_refusal(payload)
    if refusal is not None:
        return refusal
    return await _tool_result(_extracted(await _crawled(payload, settings, shutdown_event)))


async def _handle_map(payload: MapRequest, settings, shutdown_event) -> ToolResult:
    return await _tool_result(map_site(payload, settings=settings, shutdown_event=shutdown_event))


async def _handle_render(payload: RenderRequest, settings, shutdown_event) -> ToolResult:
    if get_browser_pool() is None:
        return ToolResult(
            text="Browser pool is not initialized; the service is starting up or shutting down.",
            is_error=True,
        )
    return await _tool_result(render_endpoint_handler(payload, settings=settings))


async def _handle_search(payload: SearchRequest, settings, shutdown_event) -> ToolResult:
    return await _tool_result(search_endpoint_handler(payload, settings=settings))


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


# Deterministic order: discovery primitive first, then the fetch primitives.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="search",
        title="Web search",
        description=(
            "Search the web for public URLs matching a query using the configured provider "
            "(CRAWLUTRON_SEARCH_PROVIDER). Returns SERP-style results (title, url, "
            "description) and does NOT crawl result URLs. Result titles and snippets are "
            "provider-supplied and unverified; pipe result URLs into the crawl tool."
        ),
        request_model=SearchRequest,
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
        request_model=CrawlRequest,
        unsupported_fields=_UNSUPPORTED_CRAWL_FIELDS,
        output_schema=_schema(UnifiedCrawlResponse),
        handler=_handle_crawl,
    ),
    ToolSpec(
        name="crawl_and_extract",
        title="Crawl URLs and return extracted content",
        description=(
            "Crawl one or more public HTTP(S) URLs and return each page's content rather than "
            "the full crawl envelope: canonical URL, status, classification, title and markdown. "
            "Same request shape and policy as the crawl tool. Use this when you want the content "
            "of many pages; use crawl when you need the provenance -- attempted strategies, "
            "classification evidence, diagnostics, discovered links."
        ),
        request_model=CrawlRequest,
        unsupported_fields=_UNSUPPORTED_CRAWL_FIELDS,
        output_schema=_schema(CrawlAndExtractResponse),
        handler=_handle_crawl_and_extract,
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
        request_model=MapRequest,
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
        request_model=RenderRequest,
        output_schema=_schema(UnifiedCrawlResponse),
        handler=_handle_render,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

__all__ = ["TOOLS_BY_NAME", "TOOL_SPECS", "ToolResult", "ToolSpec"]
