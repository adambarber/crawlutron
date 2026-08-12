"""Agent hints discovery endpoint."""

from typing import Any

from fastapi import APIRouter

from app.api_models import AgentHintsResponse, CrawlStrategy
from app.settings import CrawlerSettings


from app.api_models import API_VERSION, POLICY_SCHEMA_VERSION

STRATEGY_DESCRIPTIONS = {
    CrawlStrategy.HTTP_DECLARED_CRAWLER: "Direct HTTP using a transparent crawler user agent and caller-approved headers.",
    CrawlStrategy.HTTP_SITE_COMPAT: "Direct HTTP using browser-like compatibility headers before escalating to browser rendering.",
    CrawlStrategy.HTTP_CFFI_IMPERSONATE: "TLS-fingerprint-impersonated HTTP via curl_cffi with chrome124 impersonation for WAF/Cloudflare bypass.",
    CrawlStrategy.BROWSER_HEADLESS: "Firefox rendering in headless mode using Camoufox for JavaScript-dependent pages.",
    CrawlStrategy.BROWSER_HEADED_WARMED: "Headed Firefox with configured warmup URLs for challenge-prone municipal systems.",
    CrawlStrategy.DOCUMENT_EXTRACT: "Document/body extraction for PDFs and non-HTML responses when text evidence is needed.",
    CrawlStrategy.TRAFILATURA_EXTRACT: "HTML-only main-text extraction strategy for noisy HTML. It does not parse PDF or binary documents; use DOCUMENT_EXTRACT for PDFs.",
}

GUIDANCE = [
    "Use GET /health for lightweight process liveness and Docker healthchecks.",
    "Use GET /ready for crawl capacity/readiness; HTTP 503 means saturated backpressure, not necessarily crashed.",
    "Long direct HTTP, document extraction, Trafilatura, and synchronous browser work are offloaded from the FastAPI event loop so liveness can stay responsive during crawls.",
    "Prefer direct transparent crawler HTTP for sites that allow it.",
    "Use browser strategies only when direct HTTP is blocked, incomplete, or requires rendering.",
    "Do not treat HTTP 200 as content success without checking classification and quality score.",
    "Inspect classification_evidence before turning block classifications into host policy changes.",
    "Preserve raw evidence and provenance in downstream artifacts.",
    "Do not mutate host policy automatically from a crawl result.",
    "Use attempt history to generate reviewable policy suggestions.",
    "Treat discovered_links as bounded extraction output only; callers own frontier queueing, dedupe, and persistence.",
    "Inspect diagnostics.frames for legacy frameset and iframe URLs; the service reports frames but does not recursively crawl them.",
    "Inspect diagnostics.forms for form actions, methods, targets, and controls; the service reports forms but does not automatically submit them.",
    "Treat discovered pagination links as URL evidence only; callers must preserve form/search state before enqueueing paginated result URLs.",
    "Use discovered_link_count and discovered_links_truncated to decide whether to request a higher max_discovered_links limit.",
    "Do not bypass Cloudflare, WAF, CAPTCHA, login, paywall, or access-denied controls; report those outcomes as classifications and evidence.",
    "Do not solve CAPTCHAs, do not rotate proxies, do not rotate identities, do not inject credentials, and do not continue after explicit denial.",
    "Passive challenge waiting only observes whether a public page resolves within the configured timeout; it must not click, solve, outsource, or evade challenges.",
    "Keep Chonkie downstream of extracted text rather than part of retrieval strategy selection.",
]

EXAMPLE_POLICIES = {
    "browser_fallback": {
        "strategies": [
            "HTTP_DECLARED_CRAWLER",
            "HTTP_SITE_COMPAT",
            "HTTP_CFFI_IMPERSONATE",
            "BROWSER_HEADLESS",
        ],
        "limits": {
            "timeout_seconds": 30,
            "max_retries": 2,
            "max_discovered_links": 250,
        },
    },
    "document_extraction": {
        "strategies": ["DOCUMENT_EXTRACT"],
        "limits": {
            "timeout_seconds": 60,
            "max_retries": 1,
            "max_discovered_links": 0,
        },
    },
    "trafilatura_only": {
        "strategies": ["TRAFILATURA_EXTRACT"],
        "limits": {
            "timeout_seconds": 15,
            "max_retries": 1,
            "max_discovered_links": 100,
        },
    },
    "direct_http_only": {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 2,
            "max_discovered_links": 250,
        },
    },
}


def agent_hints_payload() -> dict[str, Any]:
    return {
        "service": "browser-crawler",
        "strategies": {strategy.value: description for strategy, description in STRATEGY_DESCRIPTIONS.items()},
        "guidance": GUIDANCE,
        "example_policies": EXAMPLE_POLICIES,
    }


def create_agent_hints_router(settings: CrawlerSettings) -> APIRouter:
    """Create agent hints router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/v1/agent-hints", response_model=AgentHintsResponse)
    async def agent_hints() -> dict[str, Any]:
        """Provide crawler guidance for agents."""
        return agent_hints_payload()

    return router
