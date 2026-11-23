"""MCP tool dispatch: each ``tools/call`` door delegates to the same endpoints
the REST doors use, returning a ``ToolResult``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.safety.json_safe import json_default
from crawlutron.search.endpoint import search as search_endpoint_handler
from crawlutron.service.admit_endpoint import admit_url
from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION, CallerQuotaAdmission, Tracking
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.crawl_endpoint_cores import delta_context_for
from crawlutron.service.crawl_executor import execute_crawl
from crawlutron.service.effects import EffectContainer, EffectKind, container_effect
from crawlutron.service.errors import DomainError
from crawlutron.service.job_cores import create_job_core
from crawlutron.service.main_helpers import crawl_tail, request_gate
from crawlutron.service.map_endpoint import map_site
from crawlutron.service.render_endpoint import render as render_endpoint_handler
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
    """Declared door data a ToolSpec carries instead of a handler."""

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
    @property
    def container(self) -> EffectContainer | None: ...


class _DoorKwargs(TypedDict, total=False):
    """Keyword set dispatch consumes to build one door: the DoorEnv slots."""

    settings: NotRequired[CrawlerSettings | None]
    shutdown_event: NotRequired[asyncio.Event | None]
    job_store: NotRequired[JobStore | None]
    watch_store: NotRequired[WatchStore | None]
    result_store: NotRequired[CrawlResultStore | None]
    concurrency_tracker: NotRequired[CallerConcurrencyTracker | None]
    caller: NotRequired[CallerIdentity | None]
    caller_auth: NotRequired[CallerQuotaAdmission | None]
    container: NotRequired[EffectContainer | None]


@dataclass(frozen=True)
class _ToolDoor:
    """Per-call door environment dispatch threads to every kind's body."""

    settings: CrawlerSettings | None = None
    shutdown_event: asyncio.Event | None = None
    job_store: JobStore | None = None
    watch_store: WatchStore | None = None
    result_store: CrawlResultStore | None = None
    concurrency_tracker: CallerConcurrencyTracker | None = None
    caller: CallerIdentity | None = None
    caller_auth: CallerQuotaAdmission | None = None
    container: EffectContainer | None = None


async def _admit_tool(
    caller: CallerIdentity | None,
    pages: int,
    *,
    tracking: Tracking,
    payload: CrawlRequest | MapRequest | None = None,
    work: Callable[[], Awaitable[ToolResult]],
    caller_auth: CallerQuotaAdmission | None = None,
) -> ToolResult:
    """Admit one fetch tool; hold the quota slot across the effect run (REST parity)."""
    port = CALLER_QUOTA_ADMISSION if caller_auth is None else caller_auth
    # request_gate OUTER, admit INNER: the slot stays held while work() runs.
    try:
        async with request_gate("mcp_request_rejected", logger=_logger):
            async with port.admit(caller, pages, tracking=tracking, payload=payload) as owner:
                # Admission decision made; the slot stays held across the effect run.
                _ = owner
                return await work()
    except DomainError as error:
        return _tool_error(error)


async def _crawled(
    payload: CrawlRequest,
    settings,
    shutdown_event,
    caller_id: str,
    result_store,
    container: EffectContainer | None = None,
):
    """Run the crawl under ``container.fetch``, then answer through the shared tail."""
    _crawl_started = time.monotonic()
    timeout_seconds = float(settings.default_timeout_seconds) if settings is not None else 30.0

    async def _crawl_op() -> ProducerEnvelope:
        return await execute_crawl(payload, settings=settings, shutdown_event=shutdown_event)

    result = await container_effect(container, EffectKind.FETCH, _crawl_op, timeout_seconds=timeout_seconds)
    delta_context = await delta_context_for(payload, caller_id, result_store)
    return await crawl_tail(
        result,
        _crawl_started,
        event="crawl_request_completed",
        logger=_logger,
        caller_id=caller_id,
        delta_context=delta_context,
        result_store=result_store,
        owner=caller_id,
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
    container: EffectContainer | None = None,
) -> ToolResult:
    """Delivery refusal, caller admission, then the crawl, in one place."""
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
                _crawled(
                    payload,
                    settings,
                    shutdown_event,
                    caller_for(caller).caller_id,
                    result_store,
                    container=container,
                ),
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
    core_kwargs = dict(door_data.core_kwargs)
    core_kwargs["result_store"] = door.result_store
    return await tool_result(core(*args, **core_kwargs))


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
        container=door.container,
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
    timeout_seconds = float(
        render_payload.options.timeout_seconds
        if render_payload.options and render_payload.options.timeout_seconds
        else settings.default_timeout_seconds
    )

    async def _render_op():
        return await render_endpoint_handler(render_payload, settings=settings)

    async with _port(door).bound_render_cookie_namespace(caller_for(door.caller)):
        return await _admit_tool(
            door.caller,
            len(render_payload.urls) if render_payload.urls is not None else 1,
            tracking=_tracking(door),
            work=lambda: tool_result(
                container_effect(
                    door.container, EffectKind.RENDER, _render_op, timeout_seconds=timeout_seconds
                )
            ),
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
    timeout_seconds = float(door.settings.default_timeout_seconds) if door.settings is not None else 30.0

    async def _map_op():
        return await map_site(map_payload, settings=door.settings, shutdown_event=door.shutdown_event)

    return await _admit_tool(
        door.caller,
        map_payload.policy.limits.max_pages,
        payload=map_payload,
        tracking=_tracking(door),
        caller_auth=door.caller_auth,
        work=lambda: tool_result(
            container_effect(door.container, EffectKind.FETCH, _map_op, timeout_seconds=timeout_seconds)
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
    """Admitted job-creation door; create_job_core enters admit once (#576)."""
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
