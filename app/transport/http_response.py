"""HTTP response models, result fields and cache serialization."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_policy_limits import CrawlPolicyLimits
from app.models.crawl_result import CrawlStrategyAttempt
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
    """One fetched response, and the header view every phase reads it through.

    The owner of "how this module keys a response's headers". It exists because
    that rule had three answers. ``_not_modified_result`` built its own
    lowercased copy; ``http_strategy_result`` built a second one; and
    ``_store_http_caches`` read ``etag``/``last-modified`` off the *raw* map with
    no lowering at all, so it was the one site whose answer depended on the
    transport having already normalized. The fetchers do lowercase today
    (``_finish_fetch``), which is why the third site has never been observed
    wrong -- it is a coincidence, not a contract, and nothing said so.

    What this buys is not tidiness. The phases used to take ``response``,
    ``headers`` and ``content_type`` as three sibling parameters, which made
    "headers from one response, content type from another" a call any of them
    could make and none could detect. Here the header map and the content type
    are read off the response they were derived from, so that pairing is not
    expressible -- there is no way to hand a phase a ``content_type`` that its
    ``headers`` does not contain, because the second is a read of the first.

    Built once per response, where the response enters the phases, so the
    lowercasing is paid once per fetch rather than once per phase that wanted a
    header. Attribute access that is not header access falls through to the
    response unchanged (:meth:`__getattr__`), so this is a header binding and not
    a second response type: it cannot fall behind the transport's shape, and it
    does not need editing when the transport grows a field.
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
        """Whether a Content-Encoding promised compression the body never got.

        Stated once because it is asked in two places that must not be able to
        disagree about the same response: the diagnostic that records it, and the
        quality ceiling that caps a result whose body is not what it claims.
        """
        return bool(getattr(self.raw, "decompression_failed", False))

    def validators(self, echoed: dict[str, str] | None = None) -> dict[str, str]:
        """The origin's ETag / Last-Modified, keyed the way a result spells them.

        ``echoed`` is the caller's own conditional headers, read for a validator
        a 304 did not repeat. :data:`app.models.EngineCrawlResult.validators`
        documents ``"etag"``/``"last_modified"``, the response headers are
        ``etag``/``last-modified``, and the caller's are ``If-None-Match``/
        ``If-Modified-Since`` -- three spellings of two headers, which is why
        they are read here rather than at each of the sites that report them.
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
    """Reconstruct a response object from a cached envelope.

    The envelope is this module's own dump: URL fields were serialized as
    raw text, so the rebuild re-admits them under the strategy's host policy
    -- the same re-admission a redirect hop performs -- rather than feeding
    text into value-typed fields.
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
    """The result fields every response-derived engine result repeats.

    The two URLs, the URL the response actually landed on, the attempt record and
    the response headers are the same four things whichever verdict the strategy
    reached -- ``not_modified`` and the full extraction both build them the same
    way. Stated at each site it is how a result ended up carrying the request's
    ``normalized_url`` under ``final_url`` on one path, which is invisible to every
    test that only checks the classification.
    """
    return {
        "original_url": ctx.original_url,
        "normalized_url": ctx.normalized_url,
        "final_url": resp.final_url,
        "attempt": attempt,
        "headers": resp.headers,
    }
