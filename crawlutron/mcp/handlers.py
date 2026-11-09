"""The MCP tool dispatch: every ``tools/call`` door and the machinery that
turns one endpoint call into a ``ToolResult``. Each door delegates to the same
pydantic models and endpoint functions the REST doors use; webhook/async
delivery is deliberately rejected over MCP (no out-of-band callback), and the
stored-record read tools answer a still-running job as a structured non-error
shaped from the typed pending state, never the HTTPException catch-all.

Behavior is data-driven: every ToolSpec declares a DoorBinding, and ONE
``dispatch`` switches on the declared kind -- no per-family handler factories.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from crawlutron.api.admit_endpoint import admit_url
from crawlutron.api.caller_auth import Tracking, admit, bound_render_cookie_namespace
from crawlutron.api.crawl_executor import execute_crawl
from crawlutron.api.job_cores import create_job_core
from crawlutron.api.main_helpers import request_gate
from crawlutron.api.map_endpoint import map_site
from crawlutron.api.render_endpoint import render as render_endpoint_handler
from crawlutron.api.response_shaping import aunified_response_from_result
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_and_extract_response import CrawlAndExtractResponse
from crawlutron.models.crawl_pending import CrawlPendingError
from crawlutron.models.extracted_page import ExtractedPage
from crawlutron.models.json_value import JsonValue
from crawlutron.safety.json_safe import json_default
from crawlutron.search.endpoint import search as search_endpoint_handler
from crawlutron.transport.browser_pool import get_browser_pool

if TYPE_CHECKING:
    from crawlutron.models.admit_request import AdmitRequest
    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron.models.job_create_request import JobCreateRequest
    from crawlutron.models.map_request import MapRequest
    from crawlutron.models.render_request import RenderRequest
    from crawlutron.models.search_request import SearchRequest

_logger = logging.getLogger(__name__)


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


async def tool_result(coro: Awaitable[BaseModel | Mapping[str, object]]) -> ToolResult:
    """Cross the MCP boundary for one tool call: run it, or turn the endpoint's
    ``HTTPException``/``ValueError`` into a tool error rather than a traceback.
    """
    try:
        result = await coro
    except (HTTPException, ValueError) as error:
        return _tool_error(error)
    return _tool_success(result)


def caller_for(caller: CallerIdentity | None) -> CallerIdentity:
    """The identity to enforce against: the resolved caller, else anonymous."""
    return caller if caller is not None else CallerIdentity()


#: One projection of a crawl response, chosen by the door the caller came through.
class _Unified(StrEnum):
    from_crawl = auto()
    from_extract = auto()


class DoorKind(StrEnum):
    """The door behavior dispatch binds from; one kind per distinct body."""

    admit = auto()
    crawl = auto()
    crawl_and_extract = auto()
    map = auto()
    render = auto()
    search = auto()
    read = auto()
    store = auto()
    create_job = auto()


@dataclass(frozen=True)
class DoorBinding:
    """The declared door data a ToolSpec carries instead of a handler.

    Per-tool injectables live here as declared, checked values (core, store,
    static refusal), never as **kwargs widened at call sites.
    """

    kind: DoorKind
    core: Callable[..., Awaitable[Any]] | None = None
    store: str | None = None
    wants_shutdown_event: bool = False
    core_kwargs: dict[str, Any] = field(default_factory=dict)
    refusal: ToolResult | None = None
    refusal_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ToolDoor:
    """The per-call door environment dispatch threads to every kind's body."""

    settings: Any = None
    shutdown_event: Any = None
    caller: CallerIdentity | None = None
    job_store: Any = None
    watch_store: Any = None
    result_store: Any = None
    concurrency_tracker: Any = None


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
        with request_gate("mcp_request_rejected", logger=_logger):
            async with admit(caller, pages, tracking=tracking, payload=payload):
                return await work()
    except HTTPException as error:
        return _tool_error(error)


async def _crawled(payload: CrawlRequest, settings, shutdown_event):
    """Run the crawl endpoint and shape it, so the door owns only the boundary."""
    return await aunified_response_from_result(
        await execute_crawl(
            payload,
            settings=settings,
            shutdown_event=shutdown_event,
        )
    )


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


async def _guarded_crawl(
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
    pages = payload.policy.limits.max_pages
    tracking = Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker)
    return await _admit_tool(
        caller,
        pages,
        payload=payload,
        tracking=tracking,
        work=lambda: tool_result(_projected(_crawled(payload, settings, shutdown_event), projection)),
    )


def _tracking(door: _ToolDoor) -> Tracking:
    """The caller tracking every admitted door composes."""
    return Tracking(job_store=door.job_store, concurrency_tracker=door.concurrency_tracker)


async def _running_answer(payload, job_store, result_store, caller, read) -> ToolResult:
    """Shape the read's three outcomes: the record, the typed pending state, and
    the 404 miss the replay doors report."""
    try:
        return _tool_success(
            await read(payload.request_id, job_store, result_store, caller_id=caller_for(caller).caller_id)
        )
    except CrawlPendingError as pending:
        return ToolResult(
            text=f"crawl {pending.request_id} is still {pending.job_status}; poll again with this tool",
            structured={
                "status": "running",
                "request_id": pending.request_id,
                "crawl_status": pending.job_status,
            },
        )
    except HTTPException as error:
        return _tool_error(error)


async def _store_tool(payload: BaseModel, door: _ToolDoor, door_data: DoorBinding) -> ToolResult:
    """One store-read/write door over its declared core: the caller scope is the
    resolved caller, ungated by quota."""
    store_name = door_data.store
    if store_name is None:
        raise ValueError("a store door declares its store")
    stores = {"job": door.job_store, "watch": door.watch_store}
    store = stores.get(store_name)
    if store is None:
        raise ValueError(f"{store_name} tools require an initialized {store_name} store")
    core = door_data.core
    if core is None:
        raise ValueError("a store door declares its core")
    args: list[object] = [payload, store, door.settings]
    if door_data.wants_shutdown_event:
        args.append(door.shutdown_event)
    args.append(caller_for(door.caller))
    return await tool_result(core(*args, **door_data.core_kwargs))


async def _crawl_door(payload: BaseModel, door: _ToolDoor, projection: _Unified) -> ToolResult:
    """Either crawl door: the same guarded, admitted crawl, differing only by projection."""
    return await _guarded_crawl(
        cast("CrawlRequest", payload),
        door.settings,
        door.shutdown_event,
        projection,
        caller=door.caller,
        concurrency_tracker=door.concurrency_tracker,
        job_store=door.job_store,
    )


async def _render_door(payload: BaseModel, door: _ToolDoor) -> ToolResult:
    """The render door: browser-pool guard, the caller's cookie namespace, admission."""
    render_payload = cast("RenderRequest", payload)
    if get_browser_pool() is None:
        return ToolResult(
            text="Browser pool is not initialized; the service is starting up or shutting down.",
            is_error=True,
        )
    requested = len(render_payload.urls) if render_payload.urls is not None else 1
    # The namespace binds around the whole admit, so the gate and the work
    # both run under it; an MCP render's harvest keys by the caller's key id.
    async with bound_render_cookie_namespace(caller_for(door.caller)):
        return await _admit_tool(
            door.caller,
            requested,
            tracking=_tracking(door),
            work=lambda: tool_result(render_endpoint_handler(render_payload, settings=door.settings)),
        )


async def _search_door(payload: BaseModel, door: _ToolDoor) -> ToolResult:
    """The search door: caller-bound through the concurrency register every sync door uses."""
    caller: CallerIdentity | None = door.caller
    return await _admit_tool(
        caller,
        0,
        tracking=_tracking(door),
        work=lambda: tool_result(
            search_endpoint_handler(
                cast("SearchRequest", payload),
                settings=door.settings,
                caller=caller_for(caller),
            )
        ),
    )


async def _map_door(payload: BaseModel, door: _ToolDoor) -> ToolResult:
    """The map door: admission over the policy's page limit, then the frontier walk."""
    map_payload = cast("MapRequest", payload)
    return await _admit_tool(
        door.caller,
        map_payload.policy.limits.max_pages,
        payload=map_payload,
        tracking=_tracking(door),
        work=lambda: tool_result(
            map_site(map_payload, settings=door.settings, shutdown_event=door.shutdown_event)
        ),
    )


async def _read_door(payload: BaseModel, door: _ToolDoor, door_data: DoorBinding) -> ToolResult:
    """The stored-record read doors: admitted, shaped from the typed pending state."""
    return await _admit_tool(
        door.caller,
        0,
        tracking=_tracking(door),
        work=lambda: _running_answer(payload, door.job_store, door.result_store, door.caller, door_data.core),
    )


async def dispatch(spec, payload: BaseModel, **door: Any) -> ToolResult:
    """The one tool-door dispatch: the declared refusal first, then the kind's body."""
    door_data = spec.door
    if door_data.refusal is not None and any(
        getattr(payload, f, None) not in (None, False) for f in door_data.refusal_fields
    ):
        return door_data.refusal
    tool_door = _ToolDoor(**door)
    kind = door_data.kind
    if kind is DoorKind.admit:
        return await tool_result(admit_url(cast("AdmitRequest", payload), settings=tool_door.settings))
    if kind is DoorKind.crawl:
        return await _crawl_door(payload, tool_door, _Unified.from_crawl)
    if kind is DoorKind.crawl_and_extract:
        return await _crawl_door(payload, tool_door, _Unified.from_extract)
    if kind is DoorKind.map:
        return await _map_door(payload, tool_door)
    if kind is DoorKind.render:
        return await _render_door(payload, tool_door)
    if kind is DoorKind.search:
        return await _search_door(payload, tool_door)
    if kind is DoorKind.read:
        return await _read_door(payload, tool_door, door_data)
    if kind is DoorKind.store:
        return await _store_tool(payload, tool_door, door_data)
    if kind is DoorKind.create_job:
        return await _create_job_tool(payload, tool_door)
    raise ValueError(f"unsupported door kind: {kind}")


async def _create_job_tool(payload: BaseModel, door: _ToolDoor) -> ToolResult:
    """The admitted job-creation door: the REST create's request_gate-free twin.

    create_job_core itself enters admit (quota + concurrency), so the door
    must not wrap it in admit a second time -- one request is one slot (#576)."""
    if door.job_store is None:
        raise ValueError("create_job requires an initialized job store")
    if door.concurrency_tracker is None:
        raise ValueError("create_job requires the caller's concurrency tracker")
    return await tool_result(
        create_job_core(
            cast("JobCreateRequest", payload),
            door.job_store,
            door.settings,
            door.shutdown_event,
            caller_for(door.caller),
            concurrency_tracker=door.concurrency_tracker,
        )
    )


__all__ = [
    "DoorBinding",
    "DoorKind",
    "ToolResult",
    "caller_for",
    "dispatch",
    "http_exception_message",
    "tool_result",
    "validation_message",
]
