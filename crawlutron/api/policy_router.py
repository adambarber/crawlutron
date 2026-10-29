"""``/v1/policy-suggestions`` URL→handler mapping.

The handler implementation lives in :mod:`crawlutron.api.policy_suggestions` so the
router stays trivial and the rollup is testable without FastAPI machinery.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from crawlutron.api.caller_auth import auth_denials, is_operator
from crawlutron.api.dependencies import CallerDep, get_result_store, get_settings
from crawlutron.api.exceptions import http_error
from crawlutron.api.policy_suggestions import policy_suggestions as policy_suggestions_handler
from crawlutron.models.policy_suggestions_response import PolicySuggestionsResponse

router = APIRouter(tags=["Crawler"], responses=auth_denials())


@router.get(
    "/v1/policy-suggestions",
    response_model=PolicySuggestionsResponse,
    summary="Propose candidate host policy rules from accumulated evidence",
    description=(
        "Roll up the blocked attempts across retained crawl records and propose "
        "candidate host policy rules, each with the classifications, evidence signatures "
        "and a bounded evidence sample behind it. A keyed caller's rollup covers only "
        "its own records; the operator token sees every caller's. This is a proposal "
        "surface: it never applies a rule, and it draws only on what the crawls "
        "recorded within the result cache's retention window."
    ),
)
async def policy_suggestions_endpoint(
    request: Request,
    caller: CallerDep,
    result_store=Depends(get_result_store),
    settings=Depends(get_settings),
) -> PolicySuggestionsResponse:
    # Anonymity is refused and the rollup is per-tenant: the cross-tenant view is
    # the operator's, mirroring the /metrics caller-detail gate.
    if not caller.authenticated:
        raise http_error(401, "invalid_api_key", "Unauthenticated: an API key is required")
    owner = None if is_operator(request) else caller.caller_id
    return await policy_suggestions_handler(result_store=result_store, settings=settings, owner=owner)


__all__ = ["router"]
