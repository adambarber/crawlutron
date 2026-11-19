"""HTTP extraction and classification of fetched content."""

from __future__ import annotations

from collections.abc import Mapping

from crawlutron.admission.render_options import policy_wants_markdown
from crawlutron.extraction.classify import classify_page_with_evidence
from crawlutron.models.classification_evidence import ClassificationEvidenceRecord
from crawlutron.models.request_profile_diagnostics import RequestProfileDiagnostics
from crawlutron.models.taxonomy import Classification
from crawlutron.transport import body_decode
from crawlutron.transport.content_pipeline import run_extraction
from crawlutron.transport.decompression import decompress_body as _decompress_body
from crawlutron.transport.http_response import ExtractedHttpContent, FetchedResponse, HttpStrategyContext
from crawlutron.vocabulary.extraction import (
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_TRAFILATURA,
)

__all__ = [
    "classify_extracted_content",
    "extract_http_phase",
    "request_profile_diagnostics",
]


def _is_html_body(content_type: str | None, body: str) -> bool:
    """Decide whether an already-decoded body is HTML: content-type says so, or
    the text itself opens like an HTML document. Mirrors the legacy content
    pipeline's sniff for a ``str`` body."""
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    return body.lstrip().lower().startswith(("<html", "<!doctype html>"))


async def extract_http_phase(ctx: HttpStrategyContext, resp: FetchedResponse) -> ExtractedHttpContent:
    """Decode the body, run the selected extractor, measure content.

    ``_is_html_body`` is the one predicate the funnel dispatches on -- not a
    second reading of the same question two functions apart.
    """
    content_encoding = resp.headers.get("content-encoding", "")
    if content_encoding:
        decompressed = _decompress_body(resp.body, content_encoding, max_output_bytes=ctx.max_content_length)
        strategy_decompression_failed = False
    else:
        decompressed = resp.body
        strategy_decompression_failed = resp.decompression_failed

    body_text = body_decode.decode_body(decompressed, resp.content_type)
    html_body = _is_html_body(resp.content_type, body_text)
    html = body_text if html_body else None
    extracted = await run_extraction(
        extraction=ctx.extraction,
        content=decompressed,
        decoded=body_text,
        content_type=resp.content_type,
        final_url=resp.final_url,
        policy=ctx.policy,
        default_method=EXTRACTION_METHOD_HTTP_HTML if html_body else EXTRACTION_METHOD_HTTP_BODY,
        html=html_body,
        markdown=policy_wants_markdown(ctx.policy),
        settings=ctx.settings,
    )
    text, markdown = extracted.text, extracted.markdown

    return ExtractedHttpContent(
        decompressed=decompressed,
        body_text=body_text,
        content_type=resp.content_type,
        html=html,
        text=text,
        markdown=markdown,
        extraction_method=extracted.extraction_method,
        strategy_decompression_failed=strategy_decompression_failed,
        content_metrics=body_decode.content_metrics(text, html),
    )


def request_profile_diagnostics(profile: Mapping[str, object]) -> RequestProfileDiagnostics:
    """The request-profile frame a crawl result carries in its diagnostics."""
    return {
        "user_agent_family": str(profile.get("user_agent_family", "generic_crawler")),
        "cookie_jar": str(profile.get("cookie_jar", "none")),
    }


def _status_evidence(
    classification: Classification, signature: str, status_code: int
) -> list[ClassificationEvidenceRecord]:
    """The one http_status evidence shape every status override emits."""
    return [
        {
            "classification": classification,
            "signature": signature,
            "source": "http_status",
            "context": f"HTTP {status_code}",
        }
    ]


# The status overrides as one table: (status, classification, signature). 403
# only bites when the page classifier read the page as rendered.
_STATUS_OVERRIDES: tuple[tuple[int, Classification, str], ...] = (
    (403, Classification.access_denied, "403"),
    (404, Classification.not_found, "404"),
    (410, Classification.gone, "410"),
    (429, Classification.rate_limited, "429"),
    # A 413 is a size refusal, not a body verdict: both transports emit
    # the synthetic octet-stream shape, so it must not read as binary_no_text.
    (413, Classification.payload_too_large, "413"),
)


def classify_extracted_content(
    ctx: HttpStrategyContext,
    resp: FetchedResponse,
    extracted: ExtractedHttpContent,
) -> tuple[Classification, list[ClassificationEvidenceRecord]]:
    """Apply status overrides to the page classifier's evidence."""
    classification, evidence = classify_page_with_evidence(extracted.html, extracted.text)
    status = resp.status_code
    override: tuple[int, Classification, str] | None = next(
        (row for row in _STATUS_OVERRIDES if row[0] == status),
        (status, Classification.site_error, str(status)) if status >= 500 else None,
    )
    if override is not None and (status != 403 or classification == "rendered"):
        classification = override[1]
        evidence = _status_evidence(classification, override[2], status)
    if ctx.extraction == EXTRACTION_TRAFILATURA and extracted.html is None and classification == "rendered":
        evidence = [
            ClassificationEvidenceRecord(
                classification=Classification.binary_no_text,
                signature="non_html_body",
                source="content_type",
                context=extracted.content_type or "",
            )
        ]
        classification = Classification.binary_no_text
    return classification, evidence
