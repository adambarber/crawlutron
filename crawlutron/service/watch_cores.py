"""The typed watch cores both doors run: REST ``/v1/watches`` and the MCP watch
tools. Every caller-facing watch operation is spelled once here so caller
scoping cannot diverge between doors; webhook delivery is REST-door-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qsl, urlsplit

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.watch_deleted_response import WatchDeletedResponse
from crawlutron.models.watch_list_response import WatchListResponse
from crawlutron.models.watch_state import WatchState
from crawlutron.safety.url_redaction import is_secret_name
from crawlutron.service.errors import ValidationRefusal, WatchLimitExceeded, WatchNotFound
from crawlutron.transport.freshness import default_clock

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.watch_create_request import WatchCreateRequest
    from crawlutron.models.watch_id_request import WatchIdRequest
    from crawlutron.models.webhook_target import WebhookTarget
    from crawlutron.storage.watch_store import WatchStore

__all__ = ["delete_watch_core", "get_watch_core", "list_watches_core", "register_watch_core"]


def _refuse_credential_shaped_query(url: str) -> None:
    """Refuse a watch URL whose query carries a credential-shaped parameter (#660).

    The raw spelling is echoed to webhook receivers, so a secret-named query
    value would leave at delivery time; refusal at creation keeps it out.
    """
    names = [name for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    secret = next((name for name in names if is_secret_name(name)), None)
    if secret:
        msg = f"watch URL query parameter {secret!r} carries credential-shaped vocabulary and is refused"
        raise ValidationRefusal(
            "validation_error",
            "Request validation failed",
            {"errors": [{"msg": msg}]},
        )


def _raise_not_found(watch_id: str) -> NoReturn:
    raise WatchNotFound(watch_id)


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
        raise ValidationRefusal(
            "validation_error",
            f"interval_seconds must be at least {interval_floor:g}s",
        )
    host_policy = HostPolicy.from_settings(settings)
    try:
        url = NormalizedUrl(payload.url, policy=host_policy)
    except ValueError as error:
        raise ValidationRefusal(
            "validation_error",
            "Request validation failed",
            {"errors": [{"msg": str(error)}]},
        ) from error
    _refuse_credential_shaped_query(url.raw)
    owner = caller.caller_id
    if await watch_store.acount_for_owner(owner) >= settings.max_watches_per_caller:
        raise WatchLimitExceeded(settings.max_watches_per_caller)
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
