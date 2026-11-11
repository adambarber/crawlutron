"""Agent hints discovery endpoint."""

from fastapi import APIRouter

from crawlutron.models.agent_hints_payload import AgentHintsPayload
from crawlutron.models.agent_hints_response import AgentHintsResponse
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.example_policy import ExamplePolicy
from crawlutron.service.policy_defaults import BROWSER_FALLBACK_POLICY, DOCUMENT_EXTRACTION_POLICY
from crawlutron.vocabulary.extraction import EXTRACTION_DESCRIPTIONS, EXTRACTION_TRAFILATURA
from crawlutron.vocabulary.identity import SERVICE_ID
from crawlutron.vocabulary.limits import LARGE_CRAWL_GUIDANCE
from crawlutron.vocabulary.strategies import CFFI_IMPERSONATE_TARGET, HTTP_DECLARED_CRAWLER

STRATEGY_DESCRIPTIONS = {
    CrawlStrategy.HTTP_DECLARED_CRAWLER: (
        "Direct HTTP using a transparent crawler user agent and caller-approved headers."
    ),
    CrawlStrategy.HTTP_CFFI_IMPERSONATE: f"TLS-fingerprint-impersonated HTTP via curl_cffi "
    f"({CFFI_IMPERSONATE_TARGET}) with browser-shaped headers, for sites that reject clients "
    f"presenting a non-browser TLS fingerprint.",
    CrawlStrategy.BROWSER_HEADLESS: (
        "Firefox rendering in headless mode using Camoufox for JavaScript-dependent pages."
    ),
    CrawlStrategy.BROWSER_HEADED_WARMED: (
        "Headed Firefox with configured warmup URLs for challenge-prone sites."
    ),
}


def _mcp_tool_guidance() -> str:
    """The MCP guidance entry, with the tool list DERIVED from TOOL_SPECS."""
    from crawlutron.mcp.registry import TOOL_SPECS

    names = ", ".join(spec.name for spec in TOOL_SPECS)
    return (
        "Use POST /mcp for MCP (2026-07-28, stateless streamable HTTP): server/discover lists "
        "supported protocol versions and capabilities, and tools/list exposes every "
        f"registered tool: {names}. "
        "crawl_and_extract is the crawl response projected to per-page content; "
        "read_crawl_result reads back a stored crawl response (status: running while the "
        "crawl is still running -- poll it), and read_crawl_trace its diagnostic spine. "
        "The MCP tools mirror the REST endpoints exactly."
    )


def _family_guidance(intro: str, names: tuple[str, ...], door: str) -> str:
    """One derived sentence naming a tool family and its REST door."""
    enumerated = ", ".join(names)
    return f"{intro}: {enumerated} -- the same caller-scoped doors the {door} REST routes serve."


def _suggestions_guidance() -> str:
    """Policy-suggestions guidance derived from TOOL_SPECS."""
    from crawlutron.mcp.registry import TOOL_SPECS

    return _family_guidance(
        "Self-tuning policy review is available over MCP too (policy_suggestions proposes candidate "
        "denied_hosts rules from the caller's blocked attempts, same rollup as GET /v1/policy-suggestions; "
        "proposals are never applied)",
        tuple(spec.name for spec in TOOL_SPECS if spec.name == "policy_suggestions"),
        "/v1/policy-suggestions",
    )


def _job_guidance() -> str:
    """Job-tool guidance derived from TOOL_SPECS."""
    from crawlutron.mcp.registry import TOOL_SPECS

    return _family_guidance(
        "Whole-site crawl jobs are managed over MCP too (create_job starts one, same admission and "
        "caller scoping as POST /v1/jobs)",
        tuple(spec.name for spec in TOOL_SPECS if spec.name.endswith(("_job", "_jobs"))),
        "/v1/jobs",
    )


def _watch_guidance() -> str:
    """Watch-tool guidance derived from TOOL_SPECS; webhook delivery is REST-only."""
    from crawlutron.mcp.registry import TOOL_SPECS

    return _family_guidance(
        "URL watches are managed over MCP too (register_watch starts a poll-only revalidation watch, "
        "same registration as POST /v1/watches, webhook delivery is REST-only)",
        tuple(spec.name for spec in TOOL_SPECS if spec.name.endswith(("_watch", "_watches"))),
        "/v1/watches",
    )


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
    _suggestions_guidance(),
    "Treat discovered_links as bounded extraction output only; the service dedupes within a request by "
    "canonical page identity but callers still own cross-request frontier queueing and persistence.",
    "Per-domain concurrency, delays, and circuit breakers are enforced within one request (each request "
    "runs its own scheduler); concurrent requests each enforce them independently, so cross-request "
    "per-domain politeness is yours to pace.",
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
    "Delta crawl: pass changed_since (epoch seconds) or baseline_request_id (a prior crawl/job/watch "
    "request id) on POST /v1/crawl; each result then carries delta_classification "
    "(new/changed/unchanged) with delta_evidence, and the envelope rolls delta_summary. The baseline is "
    "scoped to your caller identity; a missing or foreign baseline marks the summary degraded_baseline "
    "and classifies from changed_since freshness alone (baseline-only comparisons report "
    "baseline_expired), and baseline URLs absent from this run are reported removed conservatively in "
    "the summary.",
    "Run POST /v1/admit before the first request on any URL: it returns the admission verdict, the refusing "
    "classification, and the canonical URL with zero I/O (check_robots=true adds a TTL-cached robots check).",
    "Search result titles and snippets are provider-supplied and unverified; treat them as hints until the "
    "URL itself is crawled.",
    "The active search provider is operator-configured (CRAWLUTRON_SEARCH_PROVIDER + the matching "
    "*_API_KEY); GET /v1/capabilities reports which provider is active.",
    "Callers authenticate with an API key: present X-API-Key (or Authorization: Bearer); jobs and results "
    "created under a key are readable only under it. Anonymous requests crawl as a shared bounded identity. "
    "Key issuance/revocation (POST /v1/keys) is operator-gated and not available to callers.",
    _job_guidance(),
    _watch_guidance(),
    _mcp_tool_guidance(),
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

EXAMPLE_POLICIES: dict[str, ExamplePolicy] = {
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


def agent_hints_payload() -> AgentHintsPayload:
    return {
        "service": SERVICE_ID,
        "strategies": {
            strategy.value: description for strategy, description in STRATEGY_DESCRIPTIONS.items()
        },
        "extractions": dict(EXTRACTION_DESCRIPTIONS),
        "guidance": GUIDANCE,
        "example_policies": EXAMPLE_POLICIES,
    }


def create_agent_hints_router() -> APIRouter:
    """Create agent hints router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/v1/agent-hints", response_model=AgentHintsResponse)
    async def agent_hints() -> AgentHintsPayload:
        """Provide crawler guidance for agents."""
        return agent_hints_payload()

    return router
