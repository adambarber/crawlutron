"""``/v1/keys`` inbound caller-key issuance and inventory.

Keys authenticate callers; ownership of jobs and results is what isolates
them. The secret is shown once at issuance and never stored or listed.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.caller_auth import auth_denials
from app.api.dependencies import CallerKeyStoreDep, KeysCallerDep
from app.api.exceptions import http_error
from app.models.error_response import ErrorResponse
from app.models.issued_key import IssuedKey
from app.models.key_create_request import KeyCreateRequest
from app.models.key_summary import KeySummary
from app.models.revoked_key import RevokedKey

router = APIRouter(prefix="/v1/keys", tags=["Crawler"], responses=auth_denials())


@router.post(
    "",
    status_code=201,
    response_model=IssuedKey,
    summary="Issue an inbound caller API key",
    description=(
        "Mint a key a caller presents as X-API-Key. Jobs and results created "
        "under it are readable only under it; the secret is shown once here."
    ),
    responses={
        422: {"model": ErrorResponse, "description": "Invalid key request."},
    },
)
async def issue_key(
    payload: KeyCreateRequest,
    key_store: CallerKeyStoreDep,
    _caller: KeysCallerDep,
) -> IssuedKey:
    return await key_store.aissue_key(
        payload.label, max_pages=payload.max_pages, max_concurrency=payload.max_concurrency
    )


@router.get(
    "",
    response_model=list[KeySummary],
    summary="List live caller keys",
    description="Every live key's public row; hashes and secrets are never listed.",
    responses={},
)
async def list_keys(
    key_store: CallerKeyStoreDep,
    _caller: KeysCallerDep,
) -> list[KeySummary]:
    return [KeySummary.from_record(record) for record in await key_store.alist_keys()]


@router.delete(
    "/{key_id}",
    response_model=RevokedKey,
    summary="Revoke a caller API key",
    description="Revoke a key; its rows stay but no new request can crawl as it.",
    responses={
        404: {"model": ErrorResponse, "description": "Key not found."},
    },
)
async def revoke_key(
    key_id: str,
    key_store: CallerKeyStoreDep,
    _caller: KeysCallerDep,
) -> RevokedKey:
    if not await key_store.arevoke_key(key_id):
        raise http_error(404, "not_found", f"Unknown key_id: {key_id}")
    return RevokedKey(status="revoked", key_id=key_id)


__all__ = ["issue_key", "list_keys", "revoke_key", "router"]
