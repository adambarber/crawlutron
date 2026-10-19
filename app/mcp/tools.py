"""MCP tool registry: the four crawler primitives exposed over ``/mcp``.
Each delegates to the same pydantic models and handlers the REST endpoints use;
webhook/async delivery is deliberately rejected (no out-of-band callback).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.api.caller_auth import Tracking, admit
from app.api.crawl_executor import execute_crawl
from app.api.main_helpers import request_gate
from app.api.map_endpoint import map_site
from app.api.render_endpoint import render as render_endpoint_handler
from app.api.response_shaping import aunified_response_from_result
from app.models.caller_identity import CallerIdentity
from app.models.crawl_and_extract_response import CrawlAndExtractResponse
from app.models.crawl_request import CrawlRequest
from app.models.extracted_page import ExtractedPage
from app.models.map_request import MapRequest
from app.models.render_request import RenderRequest
from app.models.search_request import SearchRequest
from app.models.search_response import SearchResponse
from app.models.site_map_response import SiteMapResponse
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.safety.json_safe import json_default
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
    """A registered MCP tool definition plus its handler. ``request_model`` is
    the source of truth for the advertised ``inputSchema`` and argument
    validation; ``unsupported_fields`` is the same list the handler rejects on.
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


def _tool_success(value: BaseModel | Mapping[str, object]) -> ToolResult:
    dump = value.model_dump() if isinstance(value, BaseModel) else value
    return ToolResult(text=json.dumps(dump, default=json_default), structured=dump)


async def _tool_result(coro: Awaitable[BaseModel | Mapping[str, object]]) -> ToolResult:
    """Cross the MCP boundary for one tool call: run it, or turn the endpoint's
    ``HTTPException``/``ValueError`` into a tool error rather than a traceback.
    Spelled once here so no handler can forget the arm and leak a 500.
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
            payload,
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


#: One projection of a crawl response, chosen by the door the caller came through.
class _Unified(StrEnum):
    from_crawl = auto()
    from_extract = auto()


def _caller_for(caller: CallerIdentity | None) -> CallerIdentity:
    """The identity to enforce against: the resolved caller, else anonymous."""
    return caller if caller is not None else CallerIdentity()


async def _guard_then_crawl(
    payload: CrawlRequest,
    settings,
    shutdown_event,
    projection: _Unified,
    caller: CallerIdentity | None = None,
    concurrency_tracker=None,
    job_store=None,
) -> ToolResult:
    """The delivery refusal check, the caller admission, then the crawl, in one place.

    Both doors refuse webhook/async over MCP for the same reason, and the two
    refusals spelled out twice are two places a new door can forget the check.
    """
    refusal = _delivery_refusal(payload)
    if refusal is not None:
        return refusal
    pages = payload.policy.limits.max_pages
    tracking = Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker)
    return await _admit_tool(
        caller,
        pages,
        payload=payload,
        tracking=tracking,
        work=lambda: _tool_result(_projected(_crawled(payload, settings, shutdown_event), projection)),
    )


async def _projected(crawled: Awaitable[UnifiedCrawlResponse], projection: _Unified) -> BaseModel:
    """The crawl response read through the door the caller came through."""
    response = await crawled
    return response if projection is _Unified.from_crawl else await _extracted(response)


async def handle_crawl(
    payload: CrawlRequest, settings, shutdown_event, concurrency_tracker=None, caller=None, job_store=None
) -> ToolResult:
    return await _guard_then_crawl(
        payload,
        settings,
        shutdown_event,
        _Unified.from_crawl,
        caller=caller,
        concurrency_tracker=concurrency_tracker,
        job_store=job_store,
    )


async def _extracted(response: UnifiedCrawlResponse) -> CrawlAndExtractResponse:
    """The crawl response read as extraction records -- the same projection over the
    same stored data, provenance dropped, so a content-only caller does not pay
    for doubled per-page bulk on the wire.
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


async def handle_crawl_and_extract(
    payload: CrawlRequest, settings, shutdown_event, concurrency_tracker=None, caller=None, job_store=None
) -> ToolResult:
    # Both crawl doors run the same guarded, admitted crawl; the composite
    # differs only by its slimmer projection, rendered by the shared guard.
    return await _guard_then_crawl(
        payload,
        settings,
        shutdown_event,
        _Unified.from_extract,
        caller=caller,
        concurrency_tracker=concurrency_tracker,
        job_store=job_store,
    )


async def _admit_tool(
    caller: CallerIdentity | None,
    pages: int,
    *,
    tracking: Tracking,
    payload: CrawlRequest | MapRequest | None = None,
    work: Callable[[], Awaitable[ToolResult]],
) -> ToolResult:
    """Admit one fetch tool under the readiness gate: request_gate OUTER, admit INNER.

    Every fetch tool funnels through here, so the MCP surface shares the HTTP
    doors' single gate: /ready counts MCP work and the capacity cap refuses it.
    """
    # request_gate OUTER, admit INNER: the count takes the slot first, so a quota-
    # rejected request holds it only for the brief admission (the HTTP door order).
    try:
        with request_gate("mcp_request_rejected", logger=logger):
            async with admit(caller, pages, tracking=tracking, payload=payload):
                return await work()
    except HTTPException as error:
        return _tool_error(error)


async def handle_map(
    payload: MapRequest, settings, shutdown_event, concurrency_tracker=None, caller=None, job_store=None
) -> ToolResult:
    return await _admit_tool(
        caller,
        payload.policy.limits.max_pages,
        payload=payload,
        tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
        work=lambda: _tool_result(map_site(payload, settings=settings, shutdown_event=shutdown_event)),
    )


async def handle_render(
    payload: RenderRequest, settings, shutdown_event, concurrency_tracker=None, caller=None, job_store=None
) -> ToolResult:
    if get_browser_pool() is None:
        return ToolResult(
            text="Browser pool is not initialized; the service is starting up or shutting down.",
            is_error=True,
        )
    requested = len(payload.urls) if payload.urls is not None else 1
    return await _admit_tool(
        caller,
        requested,
        tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
        work=lambda: _tool_result(render_endpoint_handler(payload, settings=settings)),
    )


async def handle_search(
    payload: SearchRequest, settings, shutdown_event, concurrency_tracker=None, caller=None, job_store=None
) -> ToolResult:
    # #298 / #205 / #320: the search door is caller-bound through the one
    # concurrency register every sync door uses; a keyed caller keeps its identity.
    return await _admit_tool(
        caller,
        0,
        tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
        work=lambda: _tool_result(
            search_endpoint_handler(payload, settings=settings, caller=_caller_for(caller))
        ),
    )


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
        handler=handle_search,
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
        handler=handle_crawl,
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
        handler=handle_crawl_and_extract,
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
        handler=handle_map,
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
        handler=handle_render,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

__all__ = [
    "TOOLS_BY_NAME",
    "TOOL_SPECS",
    "ToolResult",
    "ToolSpec",
    "handle_crawl",
    "handle_crawl_and_extract",
    "handle_map",
    "handle_render",
    "handle_search",
]
