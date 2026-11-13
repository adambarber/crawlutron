"""The status-override evidence chain is pinned classification AND evidence (#330).

The five if/elif branches each set (classification, evidence); the table rewrite
must produce both byte-identically, evidence lists included.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.content_metrics import ContentMetrics
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.transport.http_classify import classify_extracted_content
from crawlutron.transport.http_response import (
    ExtractedHttpContent,
    FetchedResponse,
    HttpStrategyContext,
)
from tests._fetch_doubles import FetcherDouble
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


class _RawResponse:
    """The FetchResponseLike contract, satisfied structurally with the one
    parameterized field -- no ignore needed because the protocol is real.
    """

    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return {"content-type": "text/html"}

    @property
    def body(self) -> bytes:
        return b"<html><body>hi</body></html>"

    @property
    def final_url(self) -> NormalizedUrl:
        return NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY)

    @property
    def elapsed_ms(self) -> int:
        return 0


def _ctx() -> HttpStrategyContext:
    """The classify path reads only ``extraction``; every field is honestly typed."""
    return HttpStrategyContext(
        original_url="http://example.com/",
        normalized_url=NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY),
        strategy="HTTP_DECLARED_CRAWLER",
        policy=CrawlPolicy(),
        host_policy=UNRESTRICTED_HOST_POLICY,
        # The classify path never dispatches the fetcher; the double satisfies
        # the HttpFetcher contract so the context is honestly typed.
        http_fetcher=FetcherDouble(lambda *_: (200, b"", "text/html")),
        cache=None,
        content_cache=None,
        caller_validators=None,
        robots_sitemaps=None,
        global_concurrency=None,
        started=0.0,
        ckey=None,
    )


def _extracted() -> ExtractedHttpContent:
    return ExtractedHttpContent(
        decompressed=b"<html><body>hi</body></html>",
        body_text="hi",
        content_type="text/html",
        html="<html><body>hi</body></html>",
        text="hi",
        markdown=None,
        extraction_method="http_html",
        strategy_decompression_failed=False,
        content_metrics=ContentMetrics(),
    )


def _evidence(classification: str, signature: str, status: int) -> list[dict[str, object]]:
    return [
        {
            "classification": classification,
            "signature": signature,
            "source": "http_status",
            "context": f"HTTP {status}",
        }
    ]


_EVIDENCE = {
    403: _evidence("access_denied", "403", 403),
    404: _evidence("not_found", "404", 404),
    410: _evidence("gone", "410", 410),
    429: _evidence("rate_limited", "429", 429),
    503: _evidence("site_error", "503", 503),
}


@pytest.mark.parametrize("status", sorted(_EVIDENCE))
def test_status_override_evidence_is_pinned(status: int) -> None:
    response = FetchedResponse.of(_RawResponse(status))
    classification, evidence = classify_extracted_content(_ctx(), response, _extracted())
    assert classification == _EVIDENCE[status][0]["classification"]
    assert evidence == _EVIDENCE[status]
