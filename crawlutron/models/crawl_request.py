"""The crawl request body: one URL source plus the policy that governs the job."""

from __future__ import annotations

from typing import Annotated

from pydantic import ConfigDict, Field

from crawlutron.extraction.output_formats import OutputFormat
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.fetch_validators import FetchValidators
from crawlutron.models.single_url_source_request import SingleUrlSourceRequest

#: Policy, not schema: a caller can size a validators map freely, and unbounded is a
#: fan-out agreed to before any admission check runs.
MAX_VALIDATORS_PER_REQUEST = 64


class CrawlRequest(SingleUrlSourceRequest):
    # model used to redeclare all three, and the re-declared Field put its default in the
    # first positional slot -- the shape the checker-visibility gate exists to ban.
    model_config = ConfigDict(extra="forbid")

    policy: CrawlPolicy = Field(
        default_factory=CrawlPolicy,
        description="Resolved caller-provided policy/execution plan.",
    )
    validators: dict[str, FetchValidators] = Field(
        default_factory=dict,
        max_length=MAX_VALIDATORS_PER_REQUEST,
        description="Optional map of submitted URL -> cached validators for conditional revalidation. "
        "Keys may be the original or normalized URL. Matching URLs are fetched with "
        "If-None-Match / If-Modified-Since; a 304 produces a not_modified result with no content.",
    )
    async_mode: Annotated[
        bool,
        Field(
            description="When true, accept immediately (202) and run the crawl in the background. Results "
            "are "
            "persisted and available at /v1/crawl/{request_id}. If webhook_url is also provided, the result "
            "is delivered there on completion.",
        ),
    ] = False
    webhook_url: Annotated[
        str | None,
        Field(
            description="Optional callback URL (https:// required). When provided with async_mode, the crawl "
            "is accepted (202) and the UnifiedCrawlResponse is POSTed to this URL when complete.",
        ),
    ] = None
    output_format: Annotated[
        OutputFormat | None,
        Field(
            description="Multiple output formats for the crawl results. One of: json (default), jsonl, "
            "ndjson, markdown, csv. Non-json formats are returned as a plain Response with the matching "
            "media type instead of a UnifiedCrawlResponse model.",
        ),
    ] = None
    changed_since: Annotated[
        float | None,
        Field(
            allow_inf_nan=False,
            description="Epoch seconds; when set, the caller requests delta classification of this crawl "
            "relative to that time: results whose Last-Modified predates it classify unchanged (fail open "
            "toward change when undeterminable). Combine with baseline_request_id for fingerprint/validator "
            "classification.",
        ),
    ] = None
    baseline_request_id: Annotated[
        str | None,
        Field(
            description="Prior crawl/job/watch request id to use as the delta baseline. Scoped to the "
            "caller: a missing, expired, or foreign baseline degrades to baseline_expired (every result "
            "classifies new). Results carry delta_classification/delta_evidence and the envelope rolls "
            "delta_summary.",
        ),
    ] = None
