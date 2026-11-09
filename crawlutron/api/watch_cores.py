"""The typed watch cores both doors run: REST ``/v1/watches`` and the MCP watch tools.

Every caller-facing watch operation is spelled once here -- register, list,
get, delete -- so caller scoping cannot diverge between the HTTP door and the
MCP tool. Webhook delivery admits ONLY through the REST door: the MCP create
refuses webhook_url (no tool-delivered URL may bypass WebhookTarget/NormalizedUrl).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from crawlutron.api.exceptions import http_error, validation_http_error
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.watch_deleted_response import WatchDeletedResponse
from crawlutron.models.watch_list_response import WatchListResponse
from crawlutron.models.watch_state import WatchState
from crawlutron.transport.freshness import default_clock

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.watch_create_request import WatchCreateRequest
    from crawlutron.models.watch_id_request import WatchIdRequest
    from crawlutron.models.webhook_target import WebhookTarget
    from crawlutron.storage.watch_store import WatchStore

__all__ = ["delete_watch_core", "get_watch_core", "list_watches_core", "register_watch_core"]


def _raise_not_found(watch_id: str) -> NoReturn:
    raise http_error(404, "not_found", f"Unknown watch_id: {watch_id}")


async def register_watch_core(
    payload: WatchCreateRequest,
    watch_store: WatchStore,
    settings: CrawlerSettings,
    caller: CallerIdentity,
    *,
    webhook_url: WebhookTarget | None = None,
    interval_floor: float,
) -> WatchState:
    """Register one poll-only or REST-webhooked watch for ``caller``.

    ``webhook_url`` is an admitted WebhookTarget; the REST door builds it and
    the MCP door passes None (poll-only). Admission precedes the quota spend.
    """
    if payload.interval_seconds < interval_floor:
        raise http_error(
            422,
            "validation_error",
            f"interval_seconds must be at least {interval_floor:g}s",
        )
    host_policy = HostPolicy.from_settings(settings)
    try:
        url = NormalizedUrl(payload.url, policy=host_policy)
    except ValueError as error:
        raise validation_http_error(error) from error
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
        webhook_url=webhook_url,
        owner=owner,
    )


async def list_watches_core(
    payload, watch_store: WatchStore, settings, caller: CallerIdentity
) -> WatchListResponse:
    """The owned listing GET /v1/watches answers."""
    return WatchListResponse(watches=await watch_store.alist_for_caller(caller.caller_id))


async def get_watch_core(
    payload: WatchIdRequest, watch_store: WatchStore, settings, caller: CallerIdentity
) -> WatchState:
    """The owned state view GET /v1/watches/{watch_id} answers."""
    watch = await watch_store.aget_for_caller(payload.watch_id, caller.caller_id)
    if watch is None:
        _raise_not_found(payload.watch_id)
    return watch


async def delete_watch_core(
    payload: WatchIdRequest, watch_store: WatchStore, settings, caller: CallerIdentity
) -> WatchDeletedResponse:
    """The delete DELETE /v1/watches/{watch_id} runs; a foreign id reads a miss."""
    if not await watch_store.adelete_for_caller(payload.watch_id, caller.caller_id):
        _raise_not_found(payload.watch_id)
    return WatchDeletedResponse(deleted=True, watch_id=payload.watch_id)
