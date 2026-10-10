from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.vocabulary.limits import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    MAX_CONTENT_LENGTH_BYTES,
    MAX_DISCOVERED_LINKS,
    MAX_HTTP_TIMEOUT_SECONDS,
    MAX_JOB_MAX_RETRIES,
    MAX_RATE_LIMIT_RPS,
    MAX_RETRIES,
    MAX_TOTAL_URL_BUDGET_SECONDS,
)


class CrawlPolicyLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_seconds: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_HTTP_TIMEOUT_SECONDS,
            description="Per-strategy timeout budget in seconds. Default: 30. Applies separately to each "
            "attempted HTTP, document, or browser strategy.",
        ),
    ] = DEFAULT_HTTP_TIMEOUT_SECONDS
    max_retries: Annotated[
        int,
        Field(
            ge=0,
            le=MAX_JOB_MAX_RETRIES,
            description=(
                "Retry count for transient failures inside a selected strategy. Default: 5. "
                f"Per job, so 0-{MAX_JOB_MAX_RETRIES}; the deployment's own default is capped "
                f"lower ({MAX_RETRIES}), which bounds the blast radius of an operator setting "
                "rather than this caller's. Retries do not bypass explicit denial, CAPTCHA, WAF, "
                "login, or paywall classifications."
            ),
        ),
    ] = DEFAULT_MAX_RETRIES
    challenge_wait_seconds: Annotated[
        int,
        Field(
            ge=0,
            le=120,
            description="Passive browser challenge observation budget in seconds for browser flows. Default: "
            "15. The service waits for public content to settle but does not click, solve, outsource, evade, "
            "rotate identities, or inject credentials.",
        ),
    ] = DEFAULT_CHALLENGE_WAIT_SECONDS
    max_discovered_links: Annotated[
        int,
        Field(
            ge=0,
            le=MAX_DISCOVERED_LINKS,
            description="Bounded maximum structured discovered links returned per selected page. Default: "
            "100 (reduced from 250 to cap per-page result memory under parallel crawls). Link counts still "
            "report total discovered before truncation; callers own cross-run frontier queueing, dedupe, "
            "persistence, and recursion (the service collapses URLs that normalize to the same page within a "
            "request).",
        ),
    ] = DEFAULT_MAX_DISCOVERED_LINKS
    respect_robots: Annotated[
        bool,
        Field(
            description="When True (default), /robots.txt rules are honored before fetching and for "
            "recursive-link filtering. Set False only for domains you are authorized to crawl without robots "
            "constraints.",
        ),
    ] = True
    max_depth: Annotated[
        int,
        Field(
            ge=0,
            le=20,
            description="Bounded recursive crawl depth. 0 means a single-page crawl for both /v1/crawl and "
            "/v1/jobs. Positive values admit in-scope, robots-allowed links through that depth; durable jobs "
            "persist each frontier entry's depth.",
        ),
    ] = 0
    max_pages: Annotated[
        int,
        Field(
            ge=0,
            le=2000,
            description="Total-page bound. For /v1/crawl, 0 selects the bounded service default. For "
            "/v1/jobs, 0 or omission disables the page cap; positive values cap canonical page identities "
            "across admission, recovery, and execution. A positive cap that truncates eligible queued or "
            "discovered work sets cap_truncated; already-admitted in-flight work is preserved and recorded "
            "as cap_overcommitted.",
        ),
    ] = 0
    max_rps: Annotated[
        float,
        Field(
            ge=0.0,
            le=MAX_RATE_LIMIT_RPS,
            description="Maximum requests per second per domain enforced via token bucket. 0 disables rate "
            "limiting (default). When > 0, allows short bursts up to 5 tokens while sustaining the "
            "configured RPS.",
        ),
    ] = 0.0
    max_content_length_bytes: Annotated[
        int | None,
        Field(
            ge=0,
            le=MAX_CONTENT_LENGTH_BYTES,
            description="Maximum content size in bytes per response. Omit (recommended) to let the service "
            "scale the per-page cap against its RAM budget and concurrency; set an explicit value only when "
            "a host needs a hard cap regardless of parallelism. Capped at MAX_CONTENT_LENGTH_BYTES.",
        ),
    ] = None
    total_url_budget_seconds: Annotated[
        float,
        Field(
            ge=0.1,
            le=MAX_TOTAL_URL_BUDGET_SECONDS,
            description="Hard per-URL deadline across the whole strategy chain (seconds). A poisoned/tarpit "
            "URL that exceeds this is failed as network_error instead of holding a concurrency slot. "
            "Default: 180 (the floor of 0.1 only prevents accidental near-zero values; production configs "
            "should keep this generous).",
        ),
    ] = DEFAULT_TOTAL_URL_BUDGET_SECONDS
