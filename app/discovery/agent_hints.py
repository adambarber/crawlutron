"""Agent hints discovery endpoint."""

from typing import Any

from fastapi import APIRouter

from app.api.policy_defaults import BROWSER_FALLBACK_POLICY, DOCUMENT_EXTRACTION_POLICY
from app.api_models import (
    CrawlExtractionMethod,
    CrawlStrategy,
)
from app.api_responses import (
    AgentHintsResponse,
)
from app.constants import (
    CFFI_IMPERSONATE_TARGET,
    EXTRACTION_TRAFILATURA,
    HTTP_DECLARED_CRAWLER,
    LARGE_CRAWL_GUIDANCE,
    SERVICE_ID,
)

STRATEGY_DESCRIPTIONS = {
    CrawlStrategy.HTTP_DECLARED_CRAWLER: "Direct HTTP using a transparent crawler user agent and caller-approved headers.",  # noqa: E501
    CrawlStrategy.HTTP_CFFI_IMPERSONATE: f"TLS-fingerprint-impersonated HTTP via curl_cffi "
    f"({CFFI_IMPERSONATE_TARGET}) with browser-shaped headers, for sites that reject clients "
    f"presenting a non-browser TLS fingerprint.",
    CrawlStrategy.BROWSER_HEADLESS: "Firefox rendering in headless mode using Camoufox for JavaScript-dependent pages.",  # noqa: E501
    CrawlStrategy.BROWSER_HEADED_WARMED: "Headed Firefox with configured warmup URLs for challenge-prone municipal systems.",  # noqa: E501
}

EXTRACTION_DESCRIPTIONS = {
    CrawlExtractionMethod.FULL: "Unified output pipeline: trafilatura HTML main text, markitdown for documents, plain-text passthrough.",  # noqa: E501
    CrawlExtractionMethod.TRAFILATURA: "HTML-only main-text extraction for noisy HTML; does not parse PDF or binary documents.",  # noqa: E501
    CrawlExtractionMethod.DOCUMENT: "Document/body extraction via markitdown for PDFs and non-HTML responses when text evidence is needed.",  # noqa: E501
}

GUIDANCE = [
    "Use GET /health for lightweight process liveness and Docker healthchecks.",
    "Use GET /ready for crawl capacity/readiness; HTTP 503 means saturated backpressure, not necessarily "
    "crashed.",
    "Long direct HTTP, document extraction, trafilatura extraction, and synchronous browser work are "
    "offloaded from the FastAPI event loop so liveness can stay responsive during crawls.",
    "Prefer direct transparent crawler HTTP for sites that allow it.",
    "Use browser strategies only when direct HTTP is blocked, incomplete, or requires rendering.",
    "Do not treat HTTP 200 as content success without checking classification and quality score.",
    "Inspect classification_evidence before turning block classifications into host policy changes.",
    "Preserve raw evidence and provenance in downstream artifacts.",
    "Do not mutate host policy automatically from a crawl result.",
    "Use attempt history to generate reviewable policy suggestions.",
    "Treat discovered_links as bounded extraction output only; the service dedupes within a request by "
    "canonical page identity but callers still own cross-request frontier queueing and persistence.",
    "Never the same page twice: /v1/crawl collapses URLs that normalize to one page (host case, default "
    "port, fragment, trailing slash, UTM/tracking params, and query-param order are all canonicalized), and "
    "successful fetches are reused across requests within the content-cache TTL. Callers own cross-run "
    "dedupe and frontier persistence.",
    LARGE_CRAWL_GUIDANCE,
    "Inspect diagnostics.frames for legacy frameset and iframe URLs; the service reports frames but does not "
    "recursively crawl them.",
    "Inspect diagnostics.forms for form actions, methods, targets, and controls; the service reports forms "
    "but does not automatically submit them.",
    "Treat discovered pagination links as URL evidence only; callers must preserve form/search state before "
    "enqueueing paginated result URLs.",
    "Use POST /v1/search to discover public URLs from a query; it returns SERP-style results (title, url, "
    "description) and does NOT crawl. Pipe result URLs into POST /v1/crawl.",
    "Search result titles and snippets are provider-supplied and unverified; treat them as hints until the "
    "URL itself is crawled.",
    "The active search provider is operator-configured (CRAWLUTRON_SEARCH_PROVIDER + the matching "
    "*_API_KEY); GET /v1/capabilities reports which provider is active.",
    "Use POST /mcp for MCP (2026-07-28, stateless streamable HTTP): server/discover lists supported protocol "
    "versions and capabilities, and tools/list exposes search, crawl, map, and render. The MCP tools mirror "
    "the REST endpoints exactly.",
    "Use discovered_link_count and discovered_links_truncated to decide whether to request a higher "
    "max_discovered_links limit.",
    "Do not bypass Cloudflare, WAF, CAPTCHA, login, paywall, or access-denied controls; report those "
    "outcomes "
    "as classifications and evidence.",
    "Do not solve CAPTCHAs, do not rotate proxies, do not rotate identities, do not inject credentials, and "
    "do not continue after explicit denial.",
    "Passive challenge waiting only observes whether a public page resolves within the configured timeout; "
    "it "
    "must not click, solve, outsource, or evade challenges.",
    "Cookie-consent auto-accept is disabled by default: pass browser_profile.auto_accept_cookie_consent=true "
    "to allow consent-banner clicks on your behalf.",
    "Keep Chonkie downstream of extracted text rather than part of retrieval strategy selection.",
]

EXAMPLE_POLICIES = {
    "browser_fallback": BROWSER_FALLBACK_POLICY,
    "document_extraction": DOCUMENT_EXTRACTION_POLICY,
    "trafilatura_only": {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "extraction": EXTRACTION_TRAFILATURA,
        "limits": {
            "timeout_seconds": 15,
            "max_retries": 1,
            "max_discovered_links": 100,
        },
    },
    "direct_http_only": {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 2,
            "max_discovered_links": 250,
        },
    },
}


def agent_hints_payload() -> dict[str, Any]:
    return {
        "service": SERVICE_ID,
        "strategies": {
            strategy.value: description for strategy, description in STRATEGY_DESCRIPTIONS.items()
        },
        "extractions": {method.value: description for method, description in EXTRACTION_DESCRIPTIONS.items()},
        "guidance": GUIDANCE,
        "example_policies": EXAMPLE_POLICIES,
    }


def create_agent_hints_router() -> APIRouter:
    """Create agent hints router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/v1/agent-hints", response_model=AgentHintsResponse)
    async def agent_hints() -> dict[str, Any]:
        """Provide crawler guidance for agents."""
        return agent_hints_payload()

    return router
