"""Owns body decoding, quality scoring, and content metrics.

This module owns turning a bounded response body into text and its lightweight measurements.
It must not fetch, classify pages, or assemble crawl results.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.models.content_metrics import ContentMetrics

# Declared before the import so the ImportError fallback below is the same type
# as the symbol it stands in for; an unannotated None assignment is a type error.
_charset_from_bytes: Callable[[bytes], Any] | None

try:
    from charset_normalizer import from_bytes as _charset_from_bytes
except ImportError:
    _charset_from_bytes = None


def decode_body(body: bytes | bytearray | memoryview, content_type: str | None) -> str:
    """Decode an already-decompressed body to text: declared charset strictly, then
    charset-normalizer, then lossy UTF-8 (avoiding a whole-body replace scan). ``body`` may be
    bytearray/memoryview -- normalize to ``bytes`` here exactly once.
    """
    if not isinstance(body, bytes):
        body = bytes(body)

    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip()

    # Fast path: strict decode with the declared (or default UTF-8) charset.
    try:
        return body.decode(charset)
    except (UnicodeDecodeError, LookupError):
        pass

    # Fallback to charset-normalizer if the strict decode failed.
    if _charset_from_bytes is not None:
        detected = _charset_from_bytes(body)
        best = detected.best()
        if best is not None and best.encoding:
            return str(best)

    # Final fallback: lossy UTF-8 decode so we always return a string.
    return body.decode("utf-8", errors="replace")


def quality_score(
    status: str,
    classification: str,
    text: str | None,
    html: str | None,
    http_status: int | None,
    *,
    content_metrics: ContentMetrics | None = None,
) -> float:
    if status != "success" or classification != "rendered":
        return 0.0
    # Reuse the caller's measured html/text lengths instead of re-stripping; the
    # frameset predicate is deliberately not taken from the metrics dict.
    if content_metrics is not None:
        # Both keys are the producer's core contract; a body that measured
        # nothing falls back to the same strip the None arm does.
        text_length = content_metrics.get("visible_text_length", len((text or "").strip()))
        html_length = content_metrics.get("html_length", len((html or "").strip()))
    else:
        text_length = len((text or "").strip())
        html_length = len((html or "").strip())
    normalized_html = (html or "").lower()
    has_frameset = "<frameset" in normalized_html or "<frame" in normalized_html
    score = 0.45
    if http_status and 200 <= http_status < 300:
        score += 0.2
    if text_length >= 20:
        score += 0.25
    elif text_length > 0:
        score += 0.15
    if html_length >= 40:
        score += 0.05
    if has_frameset and text_length < 40:
        score = min(score, 0.4)
    return min(score, 1.0)


# One compiled pattern so content_metrics counts every tag of interest in a
# single linear scan instead of four separate passes.
_CONTENT_TAG_RE = re.compile(r"<(a|iframe|frame|frameset|form)\b", re.IGNORECASE)


def content_metrics(text: str | None, html: str | None) -> ContentMetrics:
    visible_text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    counts = {"a": 0, "iframe": 0, "frame": 0, "frameset": 0, "form": 0}
    for match in _CONTENT_TAG_RE.finditer(html or ""):
        counts[match.group(1).lower()] += 1
    return {
        "visible_text_length": visible_text_length,
        "html_length": html_length,
        "link_count": counts["a"],
        # frame_count historically combines <iframe> and <frame>.
        "frame_count": counts["iframe"] + counts["frame"],
        "form_count": counts["form"],
        "has_frameset": counts["frameset"] > 0,
        "text_to_html_ratio": round(visible_text_length / html_length, 4) if html_length else 0.0,
    }
