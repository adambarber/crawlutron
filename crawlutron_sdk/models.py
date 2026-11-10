"""SDK-owned pydantic v2 wire mirrors of the crawlutron service contract.

Request models use ``extra="forbid"`` except WatchCreateRequest (``extra="allow"``);
response models tolerate unknown future fields with ``extra="allow"``. The SDK does
not import crawlutron.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CrawlRequest(BaseModel):
    """Request body for ``POST /v1/crawl`` (one URL source plus a policy)."""

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str | None, Field(description="Single public HTTP(S) URL.")] = None
    urls: Annotated[
        list[str] | None,
        Field(description="Batch of public HTTP(S) URLs. Use either url or urls."),
    ] = None
    policy: dict[str, object] = Field(
        default_factory=dict,
        description="Resolved caller-provided policy/execution plan.",
    )
    validators: dict[str, object] = Field(
        default_factory=dict,
        description="Optional map of submitted URL -> cached validators.",
    )
    async_mode: bool = Field(
        default=False,
        description="When true, accept immediately (202) and run in the background.",
    )
    webhook_url: Annotated[
        str | None,
        Field(description="Optional callback URL (https:// required)."),
    ] = None
    output_format: Annotated[
        Literal["json", "jsonl", "ndjson", "markdown", "csv"] | None,
        Field(description="Output format; json is the default."),
    ] = None
    changed_since: Annotated[
        float | None,
        Field(
            ge=0,
            allow_inf_nan=False,
            description="Epoch seconds; when set, the caller requests delta classification relative to "
            "that time.",
        ),
    ] = None
    baseline_request_id: Annotated[
        str | None,
        Field(
            description="Prior crawl/job/watch request id to use as the delta baseline.",
        ),
    ] = None

    @model_validator(mode="after")
    def _require_exactly_one_url_source(self) -> CrawlRequest:
        has_url = self.url is not None
        has_urls = self.urls is not None
        if has_url == has_urls:
            raise ValueError("supply exactly one of 'url' or 'urls'")
        if has_urls and not self.urls:
            raise ValueError("'urls' must contain at least one URL")
        return self


class CrawlResponse(BaseModel):
    """The unified crawl response envelope (200 from ``POST /v1/crawl``)."""

    model_config = ConfigDict(extra="allow")

    request_id: str
    submitted_count: int
    policy_schema_version: str
    success: bool
    results: list[dict[str, object]] = Field(default_factory=list)
    delta_summary: Annotated[
        dict[str, object] | None,
        Field(
            description="Per-result delta accounting, populated when delta crawl mode is requested. "
            "Carries degraded_baseline=true when the baseline row was missing/expired/foreign, so "
            "freshness-classified results are distinguishable from baseline-lost ones.",
        ),
    ] = None


class CrawlAccepted(BaseModel):
    """The async-accepted body (202 from ``POST /v1/crawl``)."""

    model_config = ConfigDict(extra="allow")

    status: str
    request_id: str
    status_url: str
    webhook_url: Annotated[str | None, Field(default=None)] = None


class AdmitRequest(BaseModel):
    """Request body for ``POST /v1/admit`` (one URL to judge pre-flight)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="The raw URL to admit or refuse.")
    check_robots: bool = Field(
        default=False,
        description="Opt-in robots.txt check (the only I/O path).",
    )


class AdmitResponse(BaseModel):
    """The admission verdict with the per-check reason table."""

    model_config = ConfigDict(extra="allow")

    admitted: bool
    classification: Annotated[str | None, Field(default=None)] = None
    canonical_url: Annotated[str | None, Field(default=None)] = None
    checks: dict[str, str | None] = Field(default_factory=dict)


class JobCreateRequest(BaseModel):
    """Request body for ``POST /v1/jobs`` (resumable whole-site crawl job)."""

    model_config = ConfigDict(extra="forbid")

    root_url: str = Field(..., description="Root public HTTP(S) URL; stored canonically at depth 0.")
    policy: dict[str, object] = Field(
        default_factory=dict,
        description="Crawl policy (strategies, limits, scope, extraction).",
    )


class JobCreateResponse(BaseModel):
    """Response from creating a crawl job."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    root_url: str
    created_at: float


class JobStatusResponse(BaseModel):
    """Status of a crawl job."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    root_url: str
    owner: str = ""
    status: str
    kind: str
    created_at: float
    updated_at: float
    frontier_size: int
    visited_size: int
    results_count: int
    cap_truncated: bool = False
    stats: dict[str, int] = Field(default_factory=dict)
    error: Annotated[str | None, Field(default=None)] = None


class JobListResponse(BaseModel):
    """A page of crawl jobs for the caller, with the caller's total row count."""

    model_config = ConfigDict(extra="allow")

    jobs: list[JobStatusResponse] = Field(default_factory=list)
    total: int = 0


class JobControlRequest(BaseModel):
    """Control a crawl job (pause/resume/cancel)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "cancel"] = Field(..., description="One of: pause, resume, cancel.")


class JobDeletedResponse(BaseModel):
    """The delete outcome as a typed body."""

    model_config = ConfigDict(extra="allow")

    deleted: bool
    job_id: str


class SearchRequest(BaseModel):
    """Request body for ``POST /v1/search`` (web search query)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, description="Web search query.")
    limit: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=20,
            description="Maximum results to return; the server clamps this to its "
            "CRAWLUTRON_SEARCH_LIMIT_MAX (default 20).",
        ),
    ] = None


class SearchResultItem(BaseModel):
    """One SERP-style search result."""

    model_config = ConfigDict(extra="allow")

    url: str
    title: str
    description: str


class SearchResponse(BaseModel):
    """Response for ``POST /v1/search``."""

    model_config = ConfigDict(extra="allow")

    success: bool = True
    query: str
    provider: str
    limit: int
    data: list[SearchResultItem] = Field(default_factory=list)


class WatchCreateRequest(BaseModel):
    """A watch registration (POST /v1/watches)."""

    model_config = ConfigDict(extra="allow")

    url: str = Field(..., description="Public HTTP(S) URL to revalidate.")
    interval_seconds: int = Field(..., gt=0, description="Seconds between revalidation checks.")
    webhook_url: Annotated[str | None, Field(default=None)] = None


class WatchState(BaseModel):
    """One row of the watches table, as shared on the wire."""

    model_config = ConfigDict(extra="allow")

    watch_id: str
    owner: str = ""
    url: str
    webhook_url: Annotated[str | None, Field(default=None)] = None
    interval_seconds: int
    status: str = "active"
    validators: dict[str, object] = Field(default_factory=dict)
    next_check_at: float
    last_checked_at: Annotated[float | None, Field(default=None)] = None
    last_outcome: Annotated[str | None, Field(default=None)] = None
    last_changed_at: Annotated[float | None, Field(default=None)] = None
    consecutive_failures: int = 0
    created_at: float
    updated_at: float


class WatchListResponse(BaseModel):
    """Every registered watch, each with the state of its last check."""

    model_config = ConfigDict(extra="allow")

    watches: list[WatchState] = Field(default_factory=list)


class CapabilitiesResponse(BaseModel):
    """The /v1/capabilities discovery payload."""

    model_config = ConfigDict(extra="allow")

    service: str
    version: str
    policy_schema_version: str
    supported_strategies: list[str] = Field(default_factory=list)
    supported_extractions: list[str] = Field(default_factory=list)
    supported_classifications: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    default_limits: dict[str, object] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)
    crawl_timing: dict[str, object] = Field(default_factory=dict)
    search: dict[str, object] = Field(default_factory=dict)
    mcp: dict[str, object] = Field(default_factory=dict)
    openapi_url: str = ""
    swagger_ui_url: str = ""


class AgentHintsResponse(BaseModel):
    """The /v1/agent-hints guidance payload."""

    model_config = ConfigDict(extra="allow")

    service: str
    strategies: dict[str, str] = Field(default_factory=dict)
    extractions: dict[str, str] = Field(default_factory=dict)
    guidance: list[str] = Field(default_factory=list)
    example_policies: dict[str, object] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """The /health liveness response."""

    model_config = ConfigDict(extra="allow")

    status: str = "ok"


class ReadinessResponse(BaseModel):
    """The /ready crawl-capacity response."""

    model_config = ConfigDict(extra="allow")

    status: str
    active_requests: int
    max_active_requests: int
    saturated: bool


class ErrorEnvelope(BaseModel):
    """The service error envelope: {code, message, details}."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "AdmitRequest",
    "AdmitResponse",
    "AgentHintsResponse",
    "CapabilitiesResponse",
    "CrawlAccepted",
    "CrawlRequest",
    "CrawlResponse",
    "ErrorEnvelope",
    "HealthResponse",
    "JobControlRequest",
    "JobCreateRequest",
    "JobCreateResponse",
    "JobDeletedResponse",
    "JobListResponse",
    "JobStatusResponse",
    "ReadinessResponse",
    "SearchRequest",
    "SearchResponse",
    "SearchResultItem",
    "WatchCreateRequest",
    "WatchListResponse",
    "WatchState",
]
