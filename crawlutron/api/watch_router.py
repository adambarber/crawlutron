"""``/v1/watches`` registration and status.

Registration admits the URL through host policy and persists its identity; checking
belongs to the sweep (:mod:`crawlutron.api.watch_sweep`). This router owns register/list/inspect/delete.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Response

from crawlutron.api.caller_auth import auth_denials
from crawlutron.api.dependencies import CallerDep, SettingsDep, WatchStoreDep
from crawlutron.api.exceptions import http_error, validation_http_error
from crawlutron.api.watch_sweep import WATCH_SWEEP_INTERVAL_SECONDS
from crawlutron.api.webhooks import ensure_https_webhook_url, validate_webhook_url_async
from crawlutron.models.error_response import ErrorResponse
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.watch_create_request import WatchCreateRequest
from crawlutron.models.watch_list_response import WatchListResponse
from crawlutron.models.watch_state import WatchState
from crawlutron.transport.freshness import default_clock

router = APIRouter(prefix="/v1/watches", tags=["Crawler"], responses=auth_denials())


# The minimum registration interval: the sweep cadence is the natural politeness
# floor, so a sub-floor schedule is refused, not bound.
WATCH_INTERVAL_MIN_SECONDS = WATCH_SWEEP_INTERVAL_SECONDS


# ``NoReturn`` for the same reason jobs_router declares it: this always raises, so
# the checker narrows past the call site instead of demanding a downstream assert.
def _raise_not_found(watch_id: str) -> NoReturn:
    raise http_error(404, "not_found", f"Unknown watch_id: {watch_id}")


@router.post(
    "",
    summary="Register a watch and schedule its first check",
    description=(
        "Register a URL to revalidate on an interval. The sweep rechecks each due "
        "watch with the validators it already holds, so an unchanged page costs one "
        "conditional request and no body, and POSTs a notification to webhook_url "
        "when the representation changes. Omit webhook_url for a poll-only watch."
    ),
    status_code=201,
    responses={
        429: {"model": ErrorResponse, "description": "Caller watch limit exceeded."},
        422: {"model": ErrorResponse, "description": "URL or webhook this deployment will not deliver to."},
    },
)
async def create_watch(
    payload: WatchCreateRequest,
    watch_store: WatchStoreDep,
    settings: SettingsDep,
    caller: CallerDep,
) -> WatchState:
    if payload.interval_seconds < WATCH_INTERVAL_MIN_SECONDS:
        raise http_error(
            422,
            "validation_error",
            f"interval_seconds must be at least {WATCH_INTERVAL_MIN_SECONDS:g}s",
        )
    host_policy = HostPolicy.from_settings(settings)
    try:
        url = NormalizedUrl(payload.url, policy=host_policy)
        if payload.webhook_url is not None:
            # An undeliverable URL is a configuration error the caller fixes now; delivery
            # validates again regardless. ensure_https_webhook_url is the scheme law.
            ensure_https_webhook_url(payload.webhook_url)
            await validate_webhook_url_async(payload.webhook_url, host_policy=host_policy)
    except ValueError as error:
        raise validation_http_error(error) from error
    # The URL is admitted before a quota slot is spent: an invalid or denied target
    # fails fast instead of consuming the caller's per-caller watch allowance.
    owner = caller.caller_id
    if await watch_store.acount_for_owner(owner) >= settings.max_watches_per_caller:
        raise http_error(
            429,
            "watch_limit_exceeded",
            f"Caller watch limit is {settings.max_watches_per_caller}",
        )
    return await watch_store.acreate(
        url,
        interval_seconds=payload.interval_seconds,
        now=default_clock(),
        webhook_url=payload.webhook_url,
        owner=owner,
    )


@router.get(
    "",
    summary="List registered watches with live status",
    description="Return every registered watch with the state of its most recent check.",
    responses={},
)
async def list_watches(watch_store: WatchStoreDep, caller: CallerDep) -> WatchListResponse:
    return WatchListResponse(watches=await watch_store.alist_for_caller(caller.caller_id))


@router.get(
    "/{watch_id}",
    summary="Get one watch's revalidation state",
    description=(
        "Return the watch's schedule, the validators the next check will send, and "
        "what its last check concluded."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Watch not found."},
    },
)
async def get_watch(watch_id: str, watch_store: WatchStoreDep, caller: CallerDep) -> WatchState:
    watch = await watch_store.aget_for_caller(watch_id, caller.caller_id)
    if watch is None:
        _raise_not_found(watch_id)
    return watch


@router.delete(
    "/{watch_id}",
    summary="Delete a watch",
    description="Stop checking the URL. The row and its revalidation history are removed.",
    responses={
        404: {"model": ErrorResponse, "description": "Watch not found."},
    },
)
async def delete_watch(watch_id: str, watch_store: WatchStoreDep, caller: CallerDep) -> Response:
    if not await watch_store.adelete_for_caller(watch_id, caller.caller_id):
        _raise_not_found(watch_id)
    return Response(status_code=204)
