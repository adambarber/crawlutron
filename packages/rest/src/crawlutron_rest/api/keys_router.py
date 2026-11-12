"""``/v1/keys`` inbound caller-key issuance and inventory.

Keys authenticate callers; ownership of jobs and results is what isolates
them. The operator token is the only credential these routes accept.
"""

from __future__ import annotations

from crawlutron.models.error_response import ErrorResponse
from crawlutron.models.issued_key import IssuedKey
from crawlutron.models.key_create_request import KeyCreateRequest
from crawlutron.models.key_summary import KeySummary
from crawlutron.models.revoked_key import RevokedKey
from crawlutron_rest.api.dependencies import CallerKeyStoreDep
from crawlutron_rest.api.exceptions import http_error
from crawlutron_rest.api.route_layer import admin_guarded_router

router = admin_guarded_router(prefix="/v1/keys", tags=["Crawler"])


@router.post(
    "",
    status_code=201,
    response_model=IssuedKey,
    summary="Issue an inbound caller API key",
    description=(
        "Mint a key a caller presents as X-API-Key. Jobs and results created "
        "under it are readable only under it; the secret is shown once here. "
        "Requires the operator token (X-API-Key: CRAWLUTRON_KEY_ADMIN_TOKEN)."
    ),
    responses={
        422: {"model": ErrorResponse, "description": "Invalid key request."},
    },
)
async def issue_key(
    payload: KeyCreateRequest,
    key_store: CallerKeyStoreDep,
) -> IssuedKey:
    return await key_store.aissue_key(
        payload.label, max_pages=payload.max_pages, max_concurrency=payload.max_concurrency
    )


@router.get(
    "",
    response_model=list[KeySummary],
    summary="List live caller keys",
    description=(
        "Every live key's public row; hashes and secrets are never listed. "
        "Requires the operator token (X-API-Key: CRAWLUTRON_KEY_ADMIN_TOKEN)."
    ),
    responses={},
)
async def list_keys(
    key_store: CallerKeyStoreDep,
) -> list[KeySummary]:
    return [KeySummary.from_record(record) for record in await key_store.alist_keys()]


@router.delete(
    "/{key_id}",
    response_model=RevokedKey,
    summary="Revoke a caller API key",
    description=(
        "Revoke a key; its rows stay but no new request can crawl as it. "
        "Requires the operator token (X-API-Key: CRAWLUTRON_KEY_ADMIN_TOKEN)."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Key not found."},
    },
)
async def revoke_key(
    key_id: str,
    key_store: CallerKeyStoreDep,
) -> RevokedKey:
    if not await key_store.arevoke_key(key_id):
        raise http_error(404, "not_found", f"Unknown key_id: {key_id}")
    return RevokedKey(status="revoked", key_id=key_id)


__all__ = ["issue_key", "list_keys", "revoke_key", "router"]
