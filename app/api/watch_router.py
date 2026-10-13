"""``/v1/watches`` registration and status.

Registration admits the URL through this deployment's host policy and persists
its canonical identity. Everything after that belongs to the sweep
(:mod:`app.api.watch_sweep`): rechecks, recorded outcomes, and change
notifications to a watch's webhook. This router owns the edges a caller can
reach -- register, list, inspect, delete -- and nothing about checking.
"""

from __future__ import annotations

import time
from typing import NoReturn

from fastapi import APIRouter, Response

from app.api.dependencies import SettingsDep, WatchStoreDep
from app.api.exceptions import http_error, validation_http_error
from app.api.webhooks import validate_webhook_url_async
from app.models.error_response import ErrorResponse
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.watch_create_request import WatchCreateRequest
from app.models.watch_list_response import WatchListResponse
from app.models.watch_state import WatchState

router = APIRouter(prefix="/v1/watches", tags=["Crawler"])


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
        422: {"model": ErrorResponse, "description": "URL or webhook this deployment will not deliver to."},
    },
)
async def create_watch(
    payload: WatchCreateRequest,
    watch_store: WatchStoreDep,
    settings: SettingsDep,
) -> WatchState:
    host_policy = HostPolicy.from_settings(settings)
    try:
        url = NormalizedUrl(payload.url, policy=host_policy)
        if payload.webhook_url is not None:
            # An undeliverable URL is a configuration error the caller fixes now; delivery
            # validates again regardless, against a name resolving elsewhere later.
            await validate_webhook_url_async(payload.webhook_url, host_policy=host_policy)
    except ValueError as error:
        raise validation_http_error(error) from error
    return await watch_store.acreate(
        url,
        interval_seconds=payload.interval_seconds,
        now=time.time(),
        webhook_url=payload.webhook_url,
    )


@router.get(
    "",
    summary="List registered watches with live status",
    description="Return every registered watch with the state of its most recent check.",
)
async def list_watches(watch_store: WatchStoreDep) -> WatchListResponse:
    return WatchListResponse(watches=await watch_store.alist_watches())


@router.get(
    "/{watch_id}",
    summary="Get one watch's revalidation state",
    description=(
        "Return the watch's schedule, the validators the next check will send, and "
        "what its last check concluded."
    ),
    responses={404: {"model": ErrorResponse, "description": "Watch not found."}},
)
async def get_watch(watch_id: str, watch_store: WatchStoreDep) -> WatchState:
    watch = await watch_store.aget(watch_id)
    if watch is None:
        _raise_not_found(watch_id)
    return watch


@router.delete(
    "/{watch_id}",
    summary="Delete a watch",
    description="Stop checking the URL. The row and its revalidation history are removed.",
    responses={404: {"model": ErrorResponse, "description": "Watch not found."}},
)
async def delete_watch(watch_id: str, watch_store: WatchStoreDep) -> Response:
    if not await watch_store.adelete(watch_id):
        _raise_not_found(watch_id)
    return Response(status_code=204)
