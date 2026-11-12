"""The MCP tool dispatch: every ``tools/call`` door and the machinery that
turns one endpoint call into a ``ToolResult``; each door delegates to the same
pydantic models and endpoint functions the REST doors use.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Any, NotRequired, Protocol, TypedDict, Unpack, cast

from pydantic import BaseModel, ValidationError

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_pending import CrawlPendingError
from crawlutron.models.json_value import JsonValue
from crawlutron.models.policy_suggestions_response import PolicySuggestionsResponse
from crawlutron.safety.json_safe import json_default
from crawlutron.search.endpoint import search as search_endpoint_handler
from crawlutron.service.admit_endpoint import admit_url
from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION, CallerQuotaAdmission, Tracking
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.crawl_endpoint_cores import delta_context_for
from crawlutron.service.crawl_executor import execute_crawl
from crawlutron.service.errors import DomainError
from crawlutron.service.job_cores import create_job_core
from crawlutron.service.main_helpers import request_gate
from crawlutron.service.map_endpoint import map_site
from crawlutron.service.render_endpoint import render as render_endpoint_handler
from crawlutron.service.response_shaping import aunified_response_from_result
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.result_store import CrawlResultStore
from crawlutron.storage.watch_store import WatchStore
from crawlutron.transport.browser_pool import get_browser_pool
from crawlutron_mcp.crawl_shaping import CrawlProjection, projected

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


def validation_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', []))}: {err.get('msg', '')}"
            for err in error.errors()
        )
    return str(error)


def _tool_error(error: ValueError | DomainError) -> ToolResult:
    message = str(error.message) if isinstance(error, DomainError) else str(error)
    return ToolResult(text=message, is_error=True)


def _tool_success(value: BaseModel | Mapping[str, object]) -> ToolResult:
    # Validated boundary: a model dump is JSON by construction; a caller's mapping
    # crossing the wire is the JSON values it carries.
    dump = value.model_dump() if isinstance(value, BaseModel) else cast("JsonValue", dict(value))
    return ToolResult(text=json.dumps(dump, default=json_default), structured=dump)


async def tool_result(coro: Awaitable[BaseModel | Mapping[str, object]]) -> ToolResult:
    """Cross the MCP boundary for one tool call: run it, or turn the endpoint's
    ``DomainError``/``ValueError`` into a tool error rather than a traceback.
    """
    try:
        result = await coro
    except (DomainError, ValueError) as error:
        return _tool_error(error)
    return _tool_success(result)


def caller_for(caller: CallerIdentity | None) -> CallerIdentity:
    """The identity to enforce against: the resolved caller, else anonymous."""
    return caller if caller is not None else CallerIdentity()


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
    suggestions = auto()
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


class DoorEnv(Protocol):
    """The door environment every kind's body receives: seven typed slots."""

    @property
    def settings(self) -> CrawlerSettings | None: ...
    @property
    def shutdown_event(self) -> asyncio.Event | None: ...
    @property
    def caller(self) -> CallerIdentity | None: ...
    @property
    def job_store(self) -> JobStore | None: ...
    @property
    def watch_store(self) -> WatchStore | None: ...
    @property
    def result_store(self) -> CrawlResultStore | None: ...
    @property
    def concurrency_tracker(self) -> CallerConcurrencyTracker | None: ...
    @property
    def caller_auth(self) -> CallerQuotaAdmission | None: ...


class _DoorKwargs(TypedDict, total=False):
    """The keyword set dispatch consumes to build one door: the seven DoorEnv slots.

    Each defaults to None at runtime; doors built with a subset of keys still
    construct; bodies guard None where a callee requires a value.
    """

    settings: NotRequired[CrawlerSettings | None]
    shutdown_event: NotRequired[asyncio.Event | None]
    job_store: NotRequired[JobStore | None]
    watch_store: NotRequired[WatchStore | None]
    result_store: NotRequired[CrawlResultStore | None]
    concurrency_tracker: NotRequired[CallerConcurrencyTracker | None]
    caller: NotRequired[CallerIdentity | None]
    caller_auth: NotRequired[CallerQuotaAdmission | None]


@dataclass(frozen=True)
class _ToolDoor:
    """The per-call door environment dispatch threads to every kind's body.

    Same slots as before #701; every slot defaults to None.
    """

    settings: CrawlerSettings | None = None
    shutdown_event: asyncio.Event | None = None
    job_store: JobStore | None = None
    watch_store: WatchStore | None = None
    result_store: CrawlResultStore | None = None
    concurrency_tracker: CallerConcurrencyTracker | None = None
    caller: CallerIdentity | None = None
    caller_auth: CallerQuotaAdmission | None = None


async def _admit_tool(
    caller: CallerIdentity | None,
    pages: int,
    *,
    tracking: Tracking,
    payload: CrawlRequest | MapRequest | None = None,
    work: Callable[[], Awaitable[ToolResult]],
    caller_auth: CallerQuotaAdmission | None = None,
) -> ToolResult:
    """Admit one fetch tool under the readiness gate: request_gate OUTER, admit INNER.

    Every fetch tool funnels through here, so the MCP surface shares the HTTP
    doors' single gate: /ready counts MCP work and the capacity cap refuses it.
    """
    port = CALLER_QUOTA_ADMISSION if caller_auth is None else caller_auth
    # request_gate OUTER, admit INNER: the count takes the slot first, so a quota-
    # rejected request holds it only for the brief admission (the HTTP door order).
    try:
        with request_gate("mcp_request_rejected", logger=_logger):
            async with port.admit(caller, pages, tracking=tracking, payload=payload):
                return await work()
    except DomainError as error:
        return _tool_error(error)


async def _crawled(payload: CrawlRequest, settings, shutdown_event, caller_id: str, result_store):
    """Run the crawl endpoint and shape it, threading the delta knobs through the
    REST endpoint's own delta_context_for loader (one semantics source)."""
    delta_context = await delta_context_for(payload, caller_id, result_store)
    return await aunified_response_from_result(
        await execute_crawl(payload, settings=settings, shutdown_event=shutdown_event),
        delta_context=delta_context,
    )


async def _guarded_crawl(
    payload: CrawlRequest,
    settings,
    shutdown_event,
    projection: CrawlProjection,
    caller: CallerIdentity | None = None,
    concurrency_tracker=None,
    job_store=None,
    result_store=None,
    caller_auth=None,
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
        caller_auth=caller_auth,
        work=lambda: tool_result(
            projected(
                _crawled(payload, settings, shutdown_event, caller_for(caller).caller_id, result_store),
                projection,
            )
        ),
    )


def _tracking(door: DoorEnv) -> Tracking:
    """The caller tracking every admitted door composes."""
    return Tracking(job_store=door.job_store, concurrency_tracker=door.concurrency_tracker)


def _port(door: DoorEnv) -> CallerQuotaAdmission:
    """The door's injected admission port, defaulting to the module's instance."""
    return CALLER_QUOTA_ADMISSION if door.caller_auth is None else door.caller_auth


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
    except DomainError as error:
        return _tool_error(error)


async def _store_tool(payload: BaseModel, door: DoorEnv, door_data: DoorBinding) -> ToolResult:
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


async def _crawl_door(payload: BaseModel, door: DoorEnv, projection: CrawlProjection) -> ToolResult:
    """Either crawl door: the same guarded, admitted crawl, differing only by projection."""
    return await _guarded_crawl(
        cast("CrawlRequest", payload),
        door.settings,
        door.shutdown_event,
        projection,
        caller=door.caller,
        concurrency_tracker=door.concurrency_tracker,
        job_store=door.job_store,
        result_store=door.result_store,
        caller_auth=door.caller_auth,
    )


async def _render_door(payload: BaseModel, door: DoorEnv) -> ToolResult:
    """The render door: browser-pool guard, the caller's cookie namespace, admission."""
    render_payload = cast("RenderRequest", payload)
    if get_browser_pool() is None:
        return ToolResult(
            text="Browser pool is not initialized; the service is starting up or shutting down.",
            is_error=True,
        )
    settings = door.settings
    if settings is None:
        raise ValueError("render requires initialized settings")
    requested = len(render_payload.urls) if render_payload.urls is not None else 1
    # The namespace binds around the whole admit, so the gate and the work
    # both run under it; an MCP render's harvest keys by the caller's key id.
    async with _port(door).bound_render_cookie_namespace(caller_for(door.caller)):
        return await _admit_tool(
            door.caller,
            requested,
            tracking=_tracking(door),
            work=lambda: tool_result(render_endpoint_handler(render_payload, settings=settings)),
            caller_auth=door.caller_auth,
        )


async def _search_door(payload: BaseModel, door: DoorEnv) -> ToolResult:
    """The search door: caller-bound through the concurrency register every sync door uses."""
    caller: CallerIdentity | None = door.caller
    return await _admit_tool(
        caller,
        0,
        tracking=_tracking(door),
        caller_auth=door.caller_auth,
        work=lambda: tool_result(
            search_endpoint_handler(
                cast("SearchRequest", payload),
                settings=door.settings,
                caller=caller_for(caller),
            )
        ),
    )


async def _map_door(payload: BaseModel, door: DoorEnv) -> ToolResult:
    """The map door: admission over the policy's page limit, then the frontier walk."""
    map_payload = cast("MapRequest", payload)
    return await _admit_tool(
        door.caller,
        map_payload.policy.limits.max_pages,
        payload=map_payload,
        tracking=_tracking(door),
        caller_auth=door.caller_auth,
        work=lambda: tool_result(
            map_site(map_payload, settings=door.settings, shutdown_event=door.shutdown_event)
        ),
    )


async def _read_door(payload: BaseModel, door: DoorEnv, door_data: DoorBinding) -> ToolResult:
    """The stored-record read doors: admitted, shaped from the typed pending state."""
    return await _admit_tool(
        door.caller,
        0,
        tracking=_tracking(door),
        caller_auth=door.caller_auth,
        work=lambda: _running_answer(payload, door.job_store, door.result_store, door.caller, door_data.core),
    )


async def _suggestions_door(payload: BaseModel, door: DoorEnv, door_data: DoorBinding) -> ToolResult:
    """The policy-suggestions read door: the REST twin's caller scoping -- a keyed
    caller's rollup covers only its own records; anonymous is refused (REST 401,
    tool error) -- and the operator's cross-tenant view is REST-only."""
    core = door_data.core
    if core is None:
        raise ValueError("a suggestions door declares its core")
    if door.result_store is None:
        raise ValueError("policy_suggestions requires an initialized result store")
    caller = caller_for(door.caller)

    async def run() -> PolicySuggestionsResponse:
        if not caller.authenticated:
            raise ValueError("an API key is required for policy_suggestions")
        return await core(result_store=door.result_store, settings=door.settings, owner=caller.caller_id)

    return await tool_result(run())


async def dispatch(spec, payload: BaseModel, **door: Unpack[_DoorKwargs]) -> ToolResult:
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
        return await _crawl_door(payload, tool_door, CrawlProjection.from_crawl)
    if kind is DoorKind.crawl_and_extract:
        return await _crawl_door(payload, tool_door, CrawlProjection.from_extract)
    if kind is DoorKind.map:
        return await _map_door(payload, tool_door)
    if kind is DoorKind.render:
        return await _render_door(payload, tool_door)
    if kind is DoorKind.search:
        return await _search_door(payload, tool_door)
    if kind is DoorKind.read:
        return await _read_door(payload, tool_door, door_data)
    if kind is DoorKind.suggestions:
        return await _suggestions_door(payload, tool_door, door_data)
    if kind is DoorKind.store:
        return await _store_tool(payload, tool_door, door_data)
    if kind is DoorKind.create_job:
        return await _create_job_tool(payload, tool_door)
    raise ValueError(f"unsupported door kind: {kind}")


async def _create_job_tool(payload: BaseModel, door: DoorEnv) -> ToolResult:
    """The admitted job-creation door: the REST create's request_gate-free twin.

    create_job_core itself enters admit (quota + concurrency), so the door
    must not wrap it in admit a second time -- one request is one slot (#576)."""
    if door.job_store is None:
        raise ValueError("create_job requires an initialized job store")
    if door.concurrency_tracker is None:
        raise ValueError("create_job requires the caller's concurrency tracker")
    settings = door.settings
    if settings is None:
        raise ValueError("create_job requires initialized settings")
    return await tool_result(
        create_job_core(
            cast("JobCreateRequest", payload),
            door.job_store,
            settings,
            door.shutdown_event,
            caller_for(door.caller),
            concurrency_tracker=door.concurrency_tracker,
        )
    )


__all__ = [
    "DoorBinding",
    "DoorEnv",
    "DoorKind",
    "ToolResult",
    "caller_for",
    "dispatch",
    "tool_result",
    "validation_message",
]
