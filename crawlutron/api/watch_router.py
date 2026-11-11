"""``/v1/watches`` registration and status.

Registration admits the URL through host policy and persists its identity; checking
belongs to the sweep (:mod:`crawlutron.service.watch_sweep`). This router owns register/list/inspect/delete.
"""

from __future__ import annotations

from fastapi import Response

from crawlutron.api.dependencies import CallerDep, SettingsDep, WatchStoreDep
from crawlutron.api.exceptions import validation_http_error
from crawlutron.api.route_layer import guarded_router
from crawlutron.models.error_response import ErrorResponse
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.watch_create_request import WatchCreateRequest
from crawlutron.models.watch_id_request import WatchIdRequest
from crawlutron.models.watch_list_response import WatchListResponse
from crawlutron.models.watch_state import WatchState
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.service.watch_cores import (
    delete_watch_core,
    get_watch_core,
    list_watches_core,
    register_watch_core,
)
from crawlutron.service.watch_sweep import WATCH_SWEEP_INTERVAL_SECONDS
from crawlutron.service.webhooks import ensure_https_webhook_url, validate_webhook_url_async

router = guarded_router(prefix="/v1/watches", tags=["Crawler"])


# The minimum registration interval: the sweep cadence is the natural politeness
# floor, so a sub-floor schedule is refused, not bound.
WATCH_INTERVAL_MIN_SECONDS = WATCH_SWEEP_INTERVAL_SECONDS


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
    webhook_url: WebhookTarget | None
    if payload.webhook_url is not None:
        host_policy = HostPolicy.from_settings(settings)
        try:
            # The target admits through the ONE chokepoint: NormalizedUrl refuses
            # userinfo, and the pinned-request validation is the SSRF rule.
            ensure_https_webhook_url(payload.webhook_url)
            await validate_webhook_url_async(payload.webhook_url, host_policy=host_policy)
            webhook_url = WebhookTarget.admit(payload.webhook_url, host_policy=host_policy)
        except ValueError as error:
            raise validation_http_error(error) from error
    else:
        webhook_url = None
    return await register_watch_core(
        payload,
        watch_store,
        settings,
        caller,
        webhook_url=webhook_url,
        interval_floor=WATCH_INTERVAL_MIN_SECONDS,
    )


@router.get(
    "",
    summary="List registered watches with live status",
    description="Return every registered watch with the state of its most recent check.",
    responses={},
)
async def list_watches(watch_store: WatchStoreDep, caller: CallerDep) -> WatchListResponse:
    return await list_watches_core(None, watch_store, None, caller)


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
    return await get_watch_core(WatchIdRequest(watch_id=watch_id), watch_store, None, caller)


@router.delete(
    "/{watch_id}",
    summary="Delete a watch",
    description="Stop checking the URL. The row and its revalidation history are removed.",
    responses={
        404: {"model": ErrorResponse, "description": "Watch not found."},
    },
)
async def delete_watch(watch_id: str, watch_store: WatchStoreDep, caller: CallerDep) -> Response:
    await delete_watch_core(WatchIdRequest(watch_id=watch_id), watch_store, None, caller)
    return Response(status_code=204)
