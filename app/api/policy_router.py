"""``/v1/policy-suggestions`` URL→handler mapping.

The handler implementation lives in :mod:`app.api.policy_suggestions` so the
router stays trivial and the rollup is testable without FastAPI machinery.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_result_store, get_settings
from app.api.policy_suggestions import policy_suggestions as policy_suggestions_handler
from app.models.policy_suggestions_response import PolicySuggestionsResponse

router = APIRouter(tags=["Crawler"])


@router.get(
    "/v1/policy-suggestions",
    response_model=PolicySuggestionsResponse,
    summary="Propose candidate host policy rules from accumulated evidence",
    description=(
        "Roll up the blocked attempts across every retained crawl record and propose "
        "candidate host policy rules, each with the classifications, evidence signatures "
        "and a bounded evidence sample behind it. This is a proposal surface: it never "
        "applies a rule, and it draws only on what the crawls recorded within the "
        "result cache's retention window."
    ),
)
async def policy_suggestions_endpoint(
    result_store=Depends(get_result_store),
    settings=Depends(get_settings),
) -> PolicySuggestionsResponse:
    return await policy_suggestions_handler(result_store=result_store, settings=settings)


__all__ = ["router"]
