"""The MCP tool handlers: every ``tools/call`` entry point and the machinery
that turns one endpoint call into a ``ToolResult``. Each handler delegates to
the same pydantic models and endpoint functions the REST doors use; webhook/async
delivery is deliberately rejected over MCP (no out-of-band callback), and the
stored-record read tools answer a still-running job as a structured non-error
shaped from the typed pending state, never the HTTPException catch-all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from crawlutron.api.admit_endpoint import admit_url
from crawlutron.api.caller_auth import Tracking, admit, bound_render_cookie_namespace
from crawlutron.api.crawl_endpoint import read_crawl_result_record, read_crawl_trace_record
from crawlutron.api.crawl_executor import execute_crawl
from crawlutron.api.job_cores import (
    control_job,
    create_job_core,
    delete_job_core,
    get_job_core,
    list_jobs_core,
)
from crawlutron.api.main_helpers import request_gate
from crawlutron.api.map_endpoint import map_site
from crawlutron.api.render_endpoint import render as render_endpoint_handler
from crawlutron.api.response_shaping import aunified_response_from_result
from crawlutron.models.admit_request import AdmitRequest
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_and_extract_response import CrawlAndExtractResponse
from crawlutron.models.crawl_pending import CrawlPendingError
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.extracted_page import ExtractedPage
from crawlutron.models.job_create_request import JobCreateRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.safety.json_safe import json_default
from crawlutron.search.endpoint import search as search_endpoint_handler
from crawlutron.storage.job_store import JobStore
from crawlutron.transport.browser_pool import get_browser_pool

if TYPE_CHECKING:
    from crawlutron.models.crawl_result_request import CrawlResultRequest
    from crawlutron.models.map_request import MapRequest
    from crawlutron.models.render_request import RenderRequest
    from crawlutron.models.search_request import SearchRequest

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable["ToolResult"]]


@dataclass(frozen=True)
class ToolResult:
    """A ``tools/call`` outcome: text content plus optional structured data."""

    text: str
    structured: JsonValue | None = None
    is_error: bool = False


def http_exception_message(error: HTTPException) -> str:
    """Render an ``HTTPException``'s detail as the human message an MCP caller sees.

    Conforming detail (a ``dict`` with a ``message`` key) unwraps to that message;
    a plain detail is its string; neither falls back to the exception itself.
    """
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
    message = http_exception_message(error) if isinstance(error, HTTPException) else str(error)
    return ToolResult(text=message, is_error=True)


def _tool_success(value: BaseModel | Mapping[str, object]) -> ToolResult:
    # Validated boundary: a model dump is JSON by construction; a caller's mapping
    # crossing the wire is the JSON values it carries.
    dump = value.model_dump() if isinstance(value, BaseModel) else cast("JsonValue", dict(value))
    return ToolResult(text=json.dumps(dump, default=json_default), structured=dump)


async def _tool_result(coro: Awaitable[BaseModel | Mapping[str, object]]) -> ToolResult:
    """Cross the MCP boundary for one tool call: run it, or turn the endpoint's
    ``HTTPException``/``ValueError`` into a tool error rather than a traceback.
    """
    try:
        result = await coro
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(result)


def _caller_for(caller: CallerIdentity | None) -> CallerIdentity:
    """The identity to enforce against: the resolved caller, else anonymous."""
    return caller if caller is not None else CallerIdentity()


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
    """The one refusal both crawl tools make: delivery/format is REST-only.

    Keyed on the effective VALUE, not field presence: an explicit no-op
    (async_mode=false, webhook_url=null, output_format=null) is accepted.
    """
    fields = ("async_mode", "webhook_url", "output_format")
    meaningful = [field for field in fields if getattr(payload, field, None) not in (None, False)]
    if meaningful:
        return ToolResult(
            text="async_mode, webhook_url and output_format are not supported over MCP; "
            "use POST /v1/crawl for webhook/async delivery or output formats.",
            is_error=True,
        )
    return None


#: One projection of a crawl response, chosen by the door the caller came through.
class _Unified(StrEnum):
    from_crawl = auto()
    from_extract = auto()


async def _projected(crawled: Awaitable, projection: _Unified) -> BaseModel:
    """The crawl response read through the door the caller came through."""
    response = await crawled
    return response if projection is _Unified.from_crawl else await _extracted(response)


async def _extracted(response) -> CrawlAndExtractResponse:
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


def _crawl_tool_handler(projection: _Unified) -> Handler:
    """Both crawl doors run the same guarded, admitted crawl; they differ only
    by the projection the shared guard renders, so the boilerplate is spelled
    once here rather than cloned per tool."""

    async def handler(
        payload: CrawlRequest,
        settings,
        shutdown_event,
        concurrency_tracker=None,
        caller=None,
        job_store=None,
        result_store=None,
    ) -> ToolResult:
        return await _guard_then_crawl(
            payload,
            settings,
            shutdown_event,
            projection,
            caller=caller,
            concurrency_tracker=concurrency_tracker,
            job_store=job_store,
        )

    return handler


handle_crawl = _crawl_tool_handler(_Unified.from_crawl)
handle_crawl_and_extract = _crawl_tool_handler(_Unified.from_extract)


async def handle_map(
    payload: MapRequest,
    settings,
    shutdown_event,
    concurrency_tracker=None,
    caller=None,
    job_store=None,
    result_store=None,
) -> ToolResult:
    return await _admit_tool(
        caller,
        payload.policy.limits.max_pages,
        payload=payload,
        tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
        work=lambda: _tool_result(map_site(payload, settings=settings, shutdown_event=shutdown_event)),
    )


async def handle_render(
    payload: RenderRequest,
    settings,
    shutdown_event,
    concurrency_tracker=None,
    caller=None,
    job_store=None,
    result_store=None,
) -> ToolResult:
    if get_browser_pool() is None:
        return ToolResult(
            text="Browser pool is not initialized; the service is starting up or shutting down.",
            is_error=True,
        )
    requested = len(payload.urls) if payload.urls is not None else 1
    # The namespace binds around the whole admit, so the gate and the work
    # both run under it; an MCP render's harvest keys by the caller's key id.
    async with bound_render_cookie_namespace(_caller_for(caller)):
        return await _admit_tool(
            caller,
            requested,
            tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
            work=lambda: _tool_result(render_endpoint_handler(payload, settings=settings)),
        )


async def handle_admit(
    payload: AdmitRequest, settings, shutdown_event, concurrency_tracker=None, caller=None, job_store=None
) -> ToolResult:
    # The pre-flight check spends no quota and no concurrency slot: it is a pure
    # verdict (robots opt-in aside), so it does not enter the admission gate.
    return await _tool_result(admit_url(payload, settings=settings))


async def handle_search(
    payload: SearchRequest,
    settings,
    shutdown_event,
    concurrency_tracker=None,
    caller=None,
    job_store=None,
    result_store=None,
) -> ToolResult:
    # The search door is caller-bound through the one
    # concurrency register every sync door uses; a keyed caller keeps its identity.
    return await _admit_tool(
        caller,
        0,
        tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
        work=lambda: _tool_result(
            search_endpoint_handler(payload, settings=settings, caller=_caller_for(caller))
        ),
    )


def _running_result(pending: CrawlPendingError) -> ToolResult:
    """The structured NON-ERROR answer to a read that raced a running job."""
    return ToolResult(
        text=f"crawl {pending.request_id} is still {pending.job_status}; poll again with this tool",
        structured={
            "status": "running",
            "request_id": pending.request_id,
            "crawl_status": pending.job_status,
        },
    )


async def _read_stored_tool(
    payload: CrawlResultRequest,
    job_store,
    result_store,
    caller: CallerIdentity | None,
    read,
) -> ToolResult:
    """Shape the read's three outcomes: the record, the typed pending state, and
    the 404 miss the replay doors report."""
    try:
        return _tool_success(
            await read(payload.request_id, job_store, result_store, caller_id=_caller_for(caller).caller_id)
        )
    except CrawlPendingError as pending:
        return _running_result(pending)
    except HTTPException as error:
        return _tool_error(error)


def _read_tool_handler(read) -> Handler:
    """One admitted read-tool handler over a stored-record core: the two read
    tools differ only by which core they read, so the admission boilerplate is
    spelled once here rather than cloned per tool."""

    async def handler(payload: CrawlResultRequest, settings, shutdown_event, **door) -> ToolResult:
        door.setdefault("caller", None)
        tracking = Tracking(
            job_store=door.get("job_store"), concurrency_tracker=door.get("concurrency_tracker")
        )
        return await _admit_tool(
            door["caller"],
            0,
            tracking=tracking,
            work=lambda: _read_stored_tool(
                payload, door.get("job_store"), door.get("result_store"), door["caller"], read
            ),
        )

    return handler


handle_read_crawl_result = _read_tool_handler(read_crawl_result_record)
handle_read_crawl_trace = _read_tool_handler(read_crawl_trace_record)


def _job_tool_handler(core) -> Handler:
    """One job-tool handler over a store-read core: the read/control/delete job
    tools differ only by the core they run, so the handler-signature boilerplate
    is spelled once here rather than cloned per tool. Those job doors are store
    reads, not fetch executors -- the caller scope is the resolved caller,
    ungated by quota. (create_job is an admitted door and carries its own
    handler below.)"""

    async def handler(
        payload,
        settings,
        shutdown_event,
        caller=None,
        job_store=None,
        result_store=None,
        concurrency_tracker=None,
    ) -> ToolResult:
        return await _tool_result(
            core(
                payload,
                job_store,
                settings,
                shutdown_event,
                _caller_for(caller),
            )
        )

    return handler


async def _create_job_tool(
    payload: JobCreateRequest,
    settings,
    shutdown_event,
    caller=None,
    job_store: JobStore | None = None,
    result_store=None,
    concurrency_tracker=None,
) -> ToolResult:
    """The admitted job-creation door: the REST create's request_gate-free twin.

    create_job_core itself enters admit (quota + concurrency), so the handler
    must not wrap it in admit a second time -- one request is one slot (#576)."""
    if job_store is None:
        raise ValueError("create_job requires an initialized job store")
    if concurrency_tracker is None:
        raise ValueError("create_job requires the caller's concurrency tracker")
    return await _tool_result(
        create_job_core(
            payload,
            job_store,
            settings,
            shutdown_event,
            _caller_for(caller),
            concurrency_tracker=concurrency_tracker,
        )
    )


handle_list_jobs = _job_tool_handler(list_jobs_core)
handle_get_job = _job_tool_handler(get_job_core)
handle_control_job = _job_tool_handler(control_job)
handle_delete_job = _job_tool_handler(delete_job_core)
handle_create_job = _create_job_tool

__all__ = [
    "Handler",
    "handle_admit",
    "handle_control_job",
    "handle_create_job",
    "handle_crawl",
    "handle_crawl_and_extract",
    "handle_delete_job",
    "handle_get_job",
    "handle_list_jobs",
    "handle_map",
    "handle_read_crawl_result",
    "handle_read_crawl_trace",
    "handle_render",
    "handle_search",
    "http_exception_message",
    "validation_message",
]
