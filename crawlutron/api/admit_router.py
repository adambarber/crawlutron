"""``POST /v1/admit`` router: the parity-covered pre-flight door.

Pure mapping: the handler lives in ``admit_endpoint`` and this binds it to the
path the MCP ``admit`` tool's ``rest_route`` names.
"""

from __future__ import annotations

from crawlutron.api._depends_singletons import settings_dep
from crawlutron.api.admit_endpoint import admit_url
from crawlutron.api.caller_auth import quota_denials
from crawlutron.api.route_layer import guarded_router
from crawlutron.models.admit_request import AdmitRequest
from crawlutron.models.admit_response import AdmitResponse

router = guarded_router(tags=["Crawler"])


@router.post(
    "/v1/admit",
    summary="Admit a URL pre-flight: verdict plus per-check reasons",
    description=(
        "Judge one URL against the same admission checks a fetch would run, without fetching: "
        "URL validity, scheme, host policy, and the private-target egress guard are pure; "
        "opt in to the robots.txt check with check_robots=true (TTL-cached, shared with the "
        "crawl path). Returns admitted plus the refusing classification from the crawl "
        "taxonomy, the canonical URL, and a per-check breakdown. The recommended pre-flight "
        "before the first POST /v1/crawl."
    ),
    responses={
        **quota_denials(),
        422: {"description": "Malformed request body."},
    },
    response_model=AdmitResponse,
)
async def admit(
    payload: AdmitRequest,
    settings=settings_dep,
) -> AdmitResponse:
    return await admit_url(payload, settings=settings)


__all__ = ["router"]
