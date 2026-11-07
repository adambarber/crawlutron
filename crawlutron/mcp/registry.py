"""The declared MCP tool registry: the contract types and the tool list.

Every entry here is the parity gate's anchor -- each ToolSpec carries the REST
door it mirrors, so the tests/invariants/test_rest_mcp_parity table and this
registry answer each other; adding a tool means adding an entry, and deleting a
REST door means deleting the row and lowering the pinned count in the same commit.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from pydantic import BaseModel

from crawlutron.mcp.handlers import (
    Handler,
    ToolResult,
    handle_admit,
    handle_crawl,
    handle_crawl_and_extract,
    handle_map,
    handle_read_crawl_result,
    handle_read_crawl_trace,
    handle_render,
    handle_search,
)
from crawlutron.models.admit_request import AdmitRequest
from crawlutron.models.admit_response import AdmitResponse
from crawlutron.models.crawl_and_extract_response import CrawlAndExtractResponse
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result_request import CrawlResultRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.models.map_request import MapRequest
from crawlutron.models.render_request import RenderRequest
from crawlutron.models.search_request import SearchRequest
from crawlutron.models.search_response import SearchResponse
from crawlutron.models.site_map_response import SiteMapResponse
from crawlutron.models.trace_response import TraceResponse
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse

#: The request fields the crawl tool advertises *and* rejects from one list, so
#: the schema cannot offer a field the handler then refuses.
_UNSUPPORTED_CRAWL_FIELDS = frozenset({"async_mode", "webhook_url", "output_format"})


@dataclass(frozen=True)
class RestRoute:
    """The REST door an MCP tool provides parity coverage for."""

    method: str
    path: str


@dataclass(frozen=True)
class ToolSpec:
    """A registered MCP tool definition plus its handler. ``request_model`` is
    the source of truth for the advertised ``inputSchema`` and argument
    validation; ``unsupported_fields`` is the same list the handler rejects on.
    """

    name: str
    description: str
    request_model: type[BaseModel]
    output_schema: dict[str, JsonValue] | None
    handler: Handler
    title: str = ""
    unsupported_fields: Collection[str] = ()
    rest_route: RestRoute | None = None

    @property
    def input_schema(self) -> dict[str, JsonValue]:
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


def _schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    return model.model_json_schema()


# Deterministic order: admission first (the pre-flight door), then discovery,
# then the fetch primitives.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="admit",
        title="Check a URL pre-flight",
        description=(
            "Judge one URL against the same admission checks a fetch would run, without "
            "fetching it: URL validity, scheme, host policy, and the private-target egress "
            "guard are answered with zero I/O; check_robots=true opts into a robots.txt "
            "check (TTL-cached). Returns admitted, the refusing classification, the canonical "
            "URL, and a per-check breakdown. Run this before the first request on any URL you "
            "are about to crawl."
        ),
        request_model=AdmitRequest,
        output_schema=_schema(AdmitResponse),
        handler=handle_admit,
        rest_route=RestRoute("POST", "/v1/admit"),
    ),
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
        rest_route=RestRoute("POST", "/v1/search"),
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
            "available over MCP. After an async/webhook submission made over "
            "REST, poll for the stored response with read_crawl_result."
        ),
        request_model=CrawlRequest,
        unsupported_fields=_UNSUPPORTED_CRAWL_FIELDS,
        output_schema=_schema(UnifiedCrawlResponse),
        handler=handle_crawl,
        rest_route=RestRoute("POST", "/v1/crawl"),
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
        # Projection of POST /v1/crawl's response, not a distinct REST door.
        rest_route=None,
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
        rest_route=RestRoute("POST", "/v1/map"),
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
        rest_route=RestRoute("POST", "/v1/render"),
    ),
    ToolSpec(
        name="read_crawl_result",
        title="Read a stored crawl response",
        description=(
            "Retrieve the stored full crawl response for a request_id -- the same "
            "record GET /v1/crawl/{request_id} replays. While the crawl is still "
            "running, answers status: running (not an error): poll with "
            "read_crawl_result until the stored response arrives or the record "
            "expires. Use after the crawl tool, or after an async REST submission."
        ),
        request_model=CrawlResultRequest,
        output_schema=_schema(UnifiedCrawlResponse),
        handler=handle_read_crawl_result,
        rest_route=RestRoute("GET", "/v1/crawl/{request_id}"),
    ),
    ToolSpec(
        name="read_crawl_trace",
        title="Read a stored crawl trace",
        description=(
            "Read what each strategy attempted for every URL of a completed crawl "
            "-- statuses, classifications, evidence -- with no page bodies; the "
            "same record GET /v1/crawl/{request_id}/trace returns. While the crawl "
            "is still running, answers status: running; poll with read_crawl_result."
        ),
        request_model=CrawlResultRequest,
        output_schema=_schema(TraceResponse),
        handler=handle_read_crawl_trace,
        rest_route=RestRoute("GET", "/v1/crawl/{request_id}/trace"),
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

__all__ = ["TOOLS_BY_NAME", "TOOL_SPECS", "Handler", "RestRoute", "ToolResult", "ToolSpec"]
