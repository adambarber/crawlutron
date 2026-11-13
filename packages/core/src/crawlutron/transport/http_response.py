"""HTTP response models, result fields and cache serialization."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypedDict, cast

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.content_metrics import ContentMetrics
from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_policy_limits import CrawlPolicyLimits
from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
from crawlutron.models.fetch_envelope import RetryDiagnostics, SerializedResponse
from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.models.request_profile_record import RequestProfileRecord
from crawlutron.models.taxonomy import CrawlStrategyName
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache

#: The pre-fetch retry tally every strategy context starts from.
_DEFAULT_RETRY: RetryDiagnostics = {"attempts": 0, "transient_errors": [], "content_cache": "miss"}


@dataclass
class HttpStrategyContext:
    original_url: str
    normalized_url: NormalizedUrl
    strategy: CrawlStrategyName
    policy: CrawlPolicy
    host_policy: HostPolicy
    http_fetcher: HttpFetcher
    cache: HttpCache | None
    content_cache: ContentCache | None
    caller_validators: dict[str, str] | None
    robots_sitemaps: list[str] | None
    global_concurrency: int | None
    started: float
    caller_conditional: dict[str, str] = field(default_factory=dict)
    profile: RequestProfileRecord = field(default_factory=lambda: cast(RequestProfileRecord, {}))
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
    response: HttpFetchResult | None = None
    limits: CrawlPolicyLimits = field(default_factory=CrawlPolicyLimits)
    retry_diagnostics: RetryDiagnostics = field(default_factory=lambda: _DEFAULT_RETRY)
    # The crawl's resolved settings, carried so extraction never re-reads the
    # env or the cgroup per document; None keeps direct callers whole.
    settings: CrawlerSettings | None = None


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
    content_metrics: ContentMetrics


@dataclass(frozen=True)
class FetchedResponse:
    """One fetched response bound to its lowercased header view, so a phase cannot
    read a content_type that its headers lack; built once per fetch, other
    attributes falling through to the raw response typed by the protocol it satisfies.
    """

    raw: FetchResponseLike
    headers: dict[str, str]
    content_type: str | None

    @classmethod
    def of(cls, response: FetchResponseLike) -> FetchedResponse:
        """Bind ``response`` to its headers, keyed for lookup."""
        headers = {key.lower(): value for key, value in response.headers.items()}
        return cls(response, headers, headers.get("content-type"))

    @property
    def status_code(self) -> int:
        return self.raw.status_code

    @property
    def body(self) -> bytes:
        return self.raw.body

    @property
    def final_url(self) -> NormalizedUrl:
        return self.raw.final_url

    @property
    def elapsed_ms(self) -> int:
        return self.raw.elapsed_ms

    def __getattr__(self, name: str) -> object:
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


_B64_CHUNK = 3 * 64 * 1024  # a multiple of 3: no chunk carries padding, so the pieces concatenate


def _body_b64(body: bytes) -> str:
    """The body as base64, encoded in chunks so the peak stays near body + output.

    The chunked ``str +=`` append relies on CPython's in-place string growth; the
    peak IS the contract here, and test_serialize_b64_peak pins it."""
    encoded = ""
    for start in range(0, len(body), _B64_CHUNK):
        encoded += base64.b64encode(body[start : start + _B64_CHUNK]).decode("ascii")
    return encoded


def serialize_response(resp: FetchResponseLike) -> SerializedResponse:
    """Capture a fetcher response into a JSON-serializable envelope."""
    return {
        "status_code": int(resp.status_code),
        "headers": dict(resp.headers),
        "body_b64": _body_b64(resp.body),
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


class ResponseResultFields(TypedDict):
    """The key set ``response_result_fields`` returns, so a consumer spreading it
    into ``build_engine_result`` is checked key by key."""

    original_url: str
    normalized_url: NormalizedUrl
    final_url: NormalizedUrl
    attempt: CrawlStrategyAttempt
    headers: dict[str, str]


def response_result_fields(
    ctx: HttpStrategyContext, resp: FetchedResponse, attempt: CrawlStrategyAttempt
) -> ResponseResultFields:
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


class FetchResponseLike(Protocol):
    """What the code actually reads off a fetched response; HttpFetchResult is the
    reference implementation, and test doubles satisfy this structurally.
    """

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def body(self) -> bytes: ...

    @property
    def final_url(self) -> NormalizedUrl: ...

    @property
    def elapsed_ms(self) -> int: ...


#: The boundary name at call sites that take either kind of response.
FetchResponse = FetchResponseLike
