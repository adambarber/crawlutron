"""The declared MCP tool registry: the contract types and the tool list.

Every entry is the parity gate's anchor -- each ToolSpec carries the REST door
it mirrors; adding a tool means adding an entry in the same commit.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from pydantic import BaseModel

from crawlutron.api.crawl_endpoint import read_crawl_result_record, read_crawl_trace_record
from crawlutron.api.policy_suggestions import policy_suggestions as policy_suggestions_core
from crawlutron.api.watch_router import WATCH_INTERVAL_MIN_SECONDS
from crawlutron.mcp.handlers import DoorBinding, DoorKind, ToolResult
from crawlutron.models.admit_request import AdmitRequest
from crawlutron.models.admit_response import AdmitResponse
from crawlutron.models.crawl_and_extract_response import CrawlAndExtractResponse
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result_request import CrawlResultRequest
from crawlutron.models.job_control_tool_request import JobControlToolRequest
from crawlutron.models.job_create_request import JobCreateRequest
from crawlutron.models.job_create_response import JobCreateResponse
from crawlutron.models.job_deleted_response import JobDeletedResponse
from crawlutron.models.job_id_request import JobIdRequest
from crawlutron.models.job_list_request import JobListRequest
from crawlutron.models.job_list_response import JobListResponse
from crawlutron.models.job_status_response import JobStatusResponse
from crawlutron.models.json_value import JsonValue
from crawlutron.models.map_request import MapRequest
from crawlutron.models.policy_suggestions_request import PolicySuggestionsRequest
from crawlutron.models.policy_suggestions_response import PolicySuggestionsResponse
from crawlutron.models.render_request import RenderRequest
from crawlutron.models.search_request import SearchRequest
from crawlutron.models.search_response import SearchResponse
from crawlutron.models.site_map_response import SiteMapResponse
from crawlutron.models.trace_response import TraceResponse
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.models.watch_create_request import WatchCreateRequest
from crawlutron.models.watch_deleted_response import WatchDeletedResponse
from crawlutron.models.watch_id_request import WatchIdRequest
from crawlutron.models.watch_list_request import WatchListRequest
from crawlutron.models.watch_list_response import WatchListResponse
from crawlutron.models.watch_state import WatchState
from crawlutron.service.job_cores import control_job, delete_job_core, get_job_core, list_jobs_core
from crawlutron.service.watch_cores import (
    delete_watch_core,
    get_watch_core,
    list_watches_core,
    register_watch_core,
)

#: The declared MCP-side delivery refusal both crawl doors answer (keyed on
#: the fields' effective VALUE by dispatch: an explicit no-op is accepted).
_DELIVERY_REFUSAL_FIELDS = ("async_mode", "webhook_url", "output_format")

#: The schema drop-list is DERIVED from the refusal vocabulary -- one owner,
#: so the schema can never advertise a field the handler refuses.
_UNSUPPORTED_CRAWL_FIELDS = frozenset(_DELIVERY_REFUSAL_FIELDS)

_DELIVERY_REFUSAL = ToolResult(
    text="async_mode, webhook_url and output_format are not supported over MCP; "
    "use POST /v1/crawl for webhook/async delivery or output formats.",
    is_error=True,
)

#: The poll-only watch refusal: webhook delivery is admitted through REST only.
_WEBHOOK_REFUSAL = ToolResult(
    text="webhook_url is not supported over MCP: webhook delivery is admitted "
    "through the REST door POST /v1/watches only; use it without webhook_url "
    "for a poll-only watch.",
    is_error=True,
)


@dataclass(frozen=True)
class RestRoute:
    """The REST door an MCP tool provides parity coverage for."""

    method: str
    path: str


@dataclass(frozen=True)
class ToolSpec:
    """A registered MCP tool definition plus its declared door data. ``request_model`` is
    the source of truth for the advertised ``inputSchema``; ``unsupported_fields``
    is what the door refuses; ``door`` carries ``handlers.dispatch``'s data.
    """

    name: str
    description: str
    request_model: type[BaseModel]
    output_schema: dict[str, JsonValue] | None
    door: DoorBinding
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
        door=DoorBinding(kind=DoorKind.admit),
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
        door=DoorBinding(kind=DoorKind.search),
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
        door=DoorBinding(
            kind=DoorKind.crawl,
            refusal=_DELIVERY_REFUSAL,
            refusal_fields=_DELIVERY_REFUSAL_FIELDS,
        ),
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
        door=DoorBinding(
            kind=DoorKind.crawl_and_extract,
            refusal=_DELIVERY_REFUSAL,
            refusal_fields=_DELIVERY_REFUSAL_FIELDS,
        ),
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
        door=DoorBinding(kind=DoorKind.map),
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
        door=DoorBinding(kind=DoorKind.render),
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
        door=DoorBinding(kind=DoorKind.read, core=read_crawl_result_record),
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
        door=DoorBinding(kind=DoorKind.read, core=read_crawl_trace_record),
        rest_route=RestRoute("GET", "/v1/crawl/{request_id}/trace"),
    ),
    ToolSpec(
        name="create_job",
        title="Create a whole-site crawl job",
        description=(
            "Persist a resumable whole-site crawl job and immediately start executing it -- the "
            "same creation POST /v1/jobs runs. The durable runner owns depth-aware canonical "
            "frontier admission (scope, documents, analytics, robots, and max_pages); a keyed "
            "caller's job is visible only to that caller. The job is admitted against the same "
            "page and concurrency quotas as REST; max_pages rides the policy's limits and is "
            "resolved against the caller's bound. Poll get_job for live state; control_job to "
            "pause, resume, or cancel; delete_job to remove it."
        ),
        request_model=JobCreateRequest,
        output_schema=_schema(JobCreateResponse),
        door=DoorBinding(kind=DoorKind.create_job),
        rest_route=RestRoute("POST", "/v1/jobs"),
    ),
    ToolSpec(
        name="list_jobs",
        title="List crawl jobs",
        description=(
            "List the caller's whole-site crawl jobs with live status, newest first -- the same "
            "page GET /v1/jobs answers. limit pages the result, offset skips rows, and total "
            "reports the caller's full row count across every page. Use get_job for one job's "
            "full state and control_job to pause, resume, or cancel it."
        ),
        request_model=JobListRequest,
        output_schema=_schema(JobListResponse),
        door=DoorBinding(kind=DoorKind.store, core=list_jobs_core, store="job", wants_shutdown_event=True),
        rest_route=RestRoute("GET", "/v1/jobs"),
    ),
    ToolSpec(
        name="get_job",
        title="Get crawl job state and results",
        description=(
            "Return one crawl job's current status, frontier/visited/result counts, stats, and "
            "the persisted cap_truncated signal -- the same record GET /v1/jobs/{job_id} answers, "
            "visible only to the caller that owns the job."
        ),
        request_model=JobIdRequest,
        output_schema=_schema(JobStatusResponse),
        door=DoorBinding(kind=DoorKind.store, core=get_job_core, store="job", wants_shutdown_event=True),
        rest_route=RestRoute("GET", "/v1/jobs/{job_id}"),
    ),
    ToolSpec(
        name="control_job",
        title="Control a running crawl job",
        description=(
            "Pause, resume, or cancel a crawl job you own -- the same transition "
            "POST /v1/jobs/{job_id}/control runs. Pause stops the runner at the next batch "
            "boundary with the frontier retained; resume respawns the runner task from the "
            "persisted frontier; cancel is terminal. Returns the job's fresh status."
        ),
        request_model=JobControlToolRequest,
        output_schema=_schema(JobStatusResponse),
        door=DoorBinding(kind=DoorKind.store, core=control_job, store="job", wants_shutdown_event=True),
        rest_route=RestRoute("POST", "/v1/jobs/{job_id}/control"),
    ),
    ToolSpec(
        name="delete_job",
        title="Delete a crawl job",
        description=(
            "Permanently remove a crawl job you own and its data -- the same cancel-then-"
            "tombstone "
            "DELETE /v1/jobs/{job_id} runs; deleting a running job cancels its runner first. "
            "Reports whether the row was removed."
        ),
        request_model=JobIdRequest,
        output_schema=_schema(JobDeletedResponse),
        door=DoorBinding(kind=DoorKind.store, core=delete_job_core, store="job", wants_shutdown_event=True),
        rest_route=RestRoute("DELETE", "/v1/jobs/{job_id}"),
    ),
    ToolSpec(
        name="policy_suggestions",
        title="Propose candidate host policy rules",
        description=(
            "Roll up the blocked attempts across the caller's retained crawl records and propose "
            "candidate host policy rules, each with the classifications, evidence signatures and a "
            "bounded evidence sample behind it -- the same per-tenant rollup GET /v1/policy-suggestions "
            "answers. Proposal only: the service never applies a rule; review the evidence before "
            "changing host policy."
        ),
        request_model=PolicySuggestionsRequest,
        output_schema=_schema(PolicySuggestionsResponse),
        door=DoorBinding(kind=DoorKind.suggestions, core=policy_suggestions_core),
        rest_route=RestRoute("GET", "/v1/policy-suggestions"),
    ),
    ToolSpec(
        name="register_watch",
        title="Register a URL watch",
        description=(
            "Persist a poll-only watch: revalidate one URL on an interval -- the same "
            "registration POST /v1/watches runs. The sweep rechecks each due watch with "
            "the validators it already holds, so an unchanged page costs one conditional "
            "request and no body. Webhook delivery is not available over MCP: register "
            "the watch here poll-only, or use POST /v1/watches with webhook_url. Poll "
            "list_watches or get_watch for the watch's state."
        ),
        request_model=WatchCreateRequest,
        # Poll-only: the advertised schema must not offer the field the handler
        # refuses (#648 review F1); the runtime refusal stays as defense in depth.
        unsupported_fields=("webhook_url",),
        output_schema=_schema(WatchState),
        door=DoorBinding(
            kind=DoorKind.store,
            core=register_watch_core,
            store="watch",
            core_kwargs={"webhook_url": None, "interval_floor": WATCH_INTERVAL_MIN_SECONDS},
            refusal=_WEBHOOK_REFUSAL,
            refusal_fields=("webhook_url",),
        ),
        rest_route=RestRoute("POST", "/v1/watches"),
    ),
    ToolSpec(
        name="list_watches",
        title="List registered watches",
        description=(
            "List the caller's registered watches with the state of each watch's most "
            "recent check -- the same listing GET /v1/watches answers, visible only to "
            "the caller that registered them."
        ),
        request_model=WatchListRequest,
        output_schema=_schema(WatchListResponse),
        door=DoorBinding(kind=DoorKind.store, core=list_watches_core, store="watch"),
        rest_route=RestRoute("GET", "/v1/watches"),
    ),
    ToolSpec(
        name="get_watch",
        title="Get one watch's revalidation state",
        description=(
            "Return one watch's schedule, the validators the next check will send, and "
            "what its last check concluded -- the same record GET /v1/watches/{watch_id} "
            "answers, visible only to the caller that owns the watch."
        ),
        request_model=WatchIdRequest,
        output_schema=_schema(WatchState),
        door=DoorBinding(kind=DoorKind.store, core=get_watch_core, store="watch"),
        rest_route=RestRoute("GET", "/v1/watches/{watch_id}"),
    ),
    ToolSpec(
        name="delete_watch",
        title="Delete a registered watch",
        description=(
            "Stop checking a URL you watch: remove the row and its revalidation "
            "history -- the same deletion DELETE /v1/watches/{watch_id} runs."
        ),
        request_model=WatchIdRequest,
        output_schema=_schema(WatchDeletedResponse),
        door=DoorBinding(kind=DoorKind.store, core=delete_watch_core, store="watch"),
        rest_route=RestRoute("DELETE", "/v1/watches/{watch_id}"),
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

__all__ = ["TOOLS_BY_NAME", "TOOL_SPECS", "RestRoute", "ToolResult", "ToolSpec"]
