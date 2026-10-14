"""HTTP extraction and classification of fetched content."""

from __future__ import annotations

from typing import Any

from app.extraction.classify import classify_page_with_evidence
from app.extraction.content import is_html
from app.extraction.document_text import format_extracted_document_text
from app.models.taxonomy import Classification
from app.transport import body_decode
from app.transport.content_pipeline import _run_extraction
from app.transport.decompression import decompress_body as _decompress_body
from app.transport.http_response import _ExtractedHttpContent, _FetchedResponse, _HttpStrategyContext
from app.vocabulary.extraction import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_TRAFILATURA,
)


async def _extract_http_phase(ctx: _HttpStrategyContext, resp: _FetchedResponse) -> _ExtractedHttpContent:
    """Decode the body, run the selected extractor, and measure content.

    ``is_html`` is :func:`app.extraction.content.is_html`, the same predicate the
    unified funnel dispatches on, rather than a second reading of the same
    question: this copy asked "does the content type mention html, or does the
    decoded text open like a document" and extraction.py asked "the same", in
    the same package, three functions apart. Two answers to one question about
    one body is how a page that the funnel calls HTML reaches trafilatura here
    and vice versa.
    """
    content_encoding = resp.headers.get("content-encoding", "")
    if content_encoding:
        decompressed = _decompress_body(resp.body, content_encoding, max_output_bytes=ctx.max_content_length)
        strategy_decompression_failed = False
    else:
        decompressed = resp.body
        strategy_decompression_failed = resp.decompression_failed

    body_text = body_decode.decode_body(decompressed, resp.content_type)
    html_body = is_html(resp.content_type, body_text)
    html = body_text if html_body else None
    extracted = await _run_extraction(
        extraction=ctx.extraction,
        content=decompressed,
        decoded=body_text,
        content_type=resp.content_type,
        final_url=resp.final_url,
        policy=ctx.policy,
        default_method=EXTRACTION_METHOD_HTTP_HTML if html_body else EXTRACTION_METHOD_HTTP_BODY,
        html=html_body,
    )
    text, markdown = extracted.text, extracted.markdown
    if ctx.extraction == EXTRACTION_DOCUMENT and (resp.content_type or "").lower().startswith("text/"):
        text = format_extracted_document_text(text, source_url=resp.final_url)
        markdown = text

    return _ExtractedHttpContent(
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


def _request_profile_diagnostics(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "user_agent_family": str(profile.get("user_agent_family", "generic_crawler")),
        "cookie_jar": str(profile.get("cookie_jar", "none")),
    }


def _classify_extracted_content(
    ctx: _HttpStrategyContext,
    resp: _FetchedResponse,
    extracted: _ExtractedHttpContent,
) -> tuple[Classification, list[dict[str, Any]]]:
    """Apply status overrides to the page classifier's evidence."""
    classification, evidence = classify_page_with_evidence(extracted.html, extracted.text)
    if resp.status_code == 403 and classification == "rendered":
        classification = "access_denied"
        evidence = [
            {
                "classification": "access_denied",
                "signature": "403",
                "source": "http_status",
                "context": "HTTP 403",
            }
        ]
    elif resp.status_code == 404:
        classification = "not_found"
        evidence = [
            {
                "classification": "not_found",
                "signature": "404",
                "source": "http_status",
                "context": "HTTP 404",
            }
        ]
    elif resp.status_code == 410:
        classification = "gone"
        evidence = [
            {"classification": "gone", "signature": "410", "source": "http_status", "context": "HTTP 410"}
        ]
    elif resp.status_code == 429:
        classification = "rate_limited"
        evidence = [
            {
                "classification": "rate_limited",
                "signature": "429",
                "source": "http_status",
                "context": "HTTP 429",
            }
        ]
    elif resp.status_code >= 500:
        classification = "site_error"
        evidence = [
            {
                "classification": "site_error",
                "signature": str(resp.status_code),
                "source": "http_status",
                "context": f"HTTP {resp.status_code}",
            }
        ]
    if ctx.extraction == EXTRACTION_TRAFILATURA and extracted.html is None and classification == "rendered":
        classification = "binary_no_text"
        evidence = [
            {
                "classification": "binary_no_text",
                "signature": "non_html_body",
                "source": "content_type",
                "context": extracted.content_type or "",
            }
        ]
    return classification, evidence
