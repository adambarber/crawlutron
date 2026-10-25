"""HTTP response models, result fields and cache serialization."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_policy_limits import CrawlPolicyLimits
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.fetch_envelope import RetryDiagnostics, SerializedResponse
from app.models.host_policy import HostPolicy
from app.models.http_fetch_result import HttpFetchResult
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import CrawlStrategyName
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache

#: The pre-fetch retry tally every strategy context starts from.
_DEFAULT_RETRY: RetryDiagnostics = {"attempts": 0, "transient_errors": [], "content_cache": "miss"}


@dataclass
class HttpStrategyContext:
    original_url: str
    normalized_url: NormalizedUrl
    strategy: CrawlStrategyName
    policy: CrawlPolicy
    host_policy: HostPolicy
    http_fetcher: Any
    cache: HttpCache | None
    content_cache: ContentCache | None
    caller_validators: dict[str, str] | None
    robots_sitemaps: list[str] | None
    global_concurrency: int | None
    started: float
    caller_conditional: dict[str, str] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 10
    max_retries: int = 0
    max_content_length: int = 0
    base_headers: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    extraction: CrawlExtractionMethod = CrawlExtractionMethod.FULL
    include_html: bool = False
    robots_evaluated: bool = True
    ckey: str | None = None
    content_hit: bool = False
    revalidated_304: bool = False
    response: Any = None
    limits: CrawlPolicyLimits = field(default_factory=CrawlPolicyLimits)
    retry_diagnostics: RetryDiagnostics = field(default_factory=lambda: _DEFAULT_RETRY)
    # The crawl's own resolved settings, carried so extraction never re-reads
    # the env or the cgroup per document (#509); None keeps direct callers whole.
    settings: Any = None


@dataclass
class ExtractedHttpContent:
    decompressed: bytes | bytearray
    body_text: str
    content_type: str | None
    html: str | None
    text: str
    markdown: str | None
    extraction_method: str
    strategy_decompression_failed: bool
    content_metrics: dict[str, int | float | bool]


@dataclass(frozen=True)
class FetchedResponse:
    """One fetched response bound to its lowercased header view, so a phase cannot
    read a content_type that its headers lack; built once per fetch, other
    attributes falling through to the raw response.
    """

    raw: Any
    headers: dict[str, str]
    content_type: str | None

    @classmethod
    def of(cls, response: Any) -> FetchedResponse:
        """Bind ``response`` to its headers, keyed for lookup."""
        headers = {key.lower(): value for key, value in response.headers.items()}
        return cls(response, headers, headers.get("content-type"))

    def __getattr__(self, name: str) -> Any:
        # Only reached for names the three fields above do not claim;
        # ``object.__getattribute__`` keeps a missing ``raw`` from recursing here.
        return getattr(object.__getattribute__(self, "raw"), name)

    @property
    def decompression_failed(self) -> bool:
        """Whether Content-Encoding promised compression the body never got; stated
        once because it is read in two places that must not disagree about one
        response -- the recording diagnostic and the quality ceiling.
        """
        return bool(getattr(self.raw, "decompression_failed", False))

    def validators(self, echoed: dict[str, str] | None = None) -> dict[str, str]:
        """The origin's ETag / Last-Modified, keyed the way a result spells them.
        ``echoed`` is the caller's conditional headers, read for a validator a 304
        did not repeat -- three spellings converged here, not at each site.
        """
        fallback = echoed or {}
        return {
            key: value
            for key, value in (
                ("etag", self.headers.get("etag") or fallback.get("If-None-Match")),
                ("last_modified", self.headers.get("last-modified") or fallback.get("If-Modified-Since")),
            )
            if value
        }


def serialize_response(resp: Any) -> SerializedResponse:
    """Capture a fetcher response into a JSON-serializable envelope."""
    return {
        "status_code": int(resp.status_code),
        "headers": dict(resp.headers),
        "body_b64": base64.b64encode(resp.body).decode("ascii"),
        "final_url": str(resp.final_url),
        "elapsed_ms": int(getattr(resp, "elapsed_ms", 0)),
    }


def rebuild_response(data: SerializedResponse, ctx: HttpStrategyContext) -> HttpFetchResult:
    """Reconstruct a response object from a cached envelope the module dumped:
    the URL fields were serialized as raw text, so the rebuild re-admits them
    under the strategy's host policy rather than feeding text into URL fields.
    """
    return HttpFetchResult(
        url=ctx.normalized_url,
        final_url=NormalizedUrl(data["final_url"], policy=ctx.host_policy),
        status_code=data["status_code"],
        headers=data["headers"],
        body=base64.b64decode(data["body_b64"]),
        elapsed_ms=data.get("elapsed_ms", 0),
    )


def response_result_fields(
    ctx: HttpStrategyContext, resp: FetchedResponse, attempt: CrawlStrategyAttempt
) -> dict[str, Any]:
    """The result fields every response-derived engine result repeats -- the URLs,
    the landed URL, the attempt, and the headers -- whatever verdict was reached.
    Stated here once so the two builders cannot carry different bases.
    """
    return {
        "original_url": ctx.original_url,
        "normalized_url": ctx.normalized_url,
        "final_url": resp.final_url,
        "attempt": attempt,
        "headers": resp.headers,
    }
