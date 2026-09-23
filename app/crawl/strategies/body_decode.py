"""Owns body decoding, quality scoring, and content metrics.

This module owns turning a bounded response body into text and its lightweight measurements.
It must not fetch, classify pages, or assemble crawl results.
"""

from __future__ import annotations

import re

try:
    from charset_normalizer import from_bytes as _charset_from_bytes
except ImportError:
    _charset_from_bytes = None  # type: ignore[assignment]


def _decode_body(body: bytes | bytearray | memoryview, content_type: str | None) -> str:
    """Decode an already-decompressed response body to text.

    Try the declared (or UTF-8) charset strictly first; fall back to
    charset-normalizer when that fails, then to a lossy UTF-8 decode. This
    avoids scanning/rewriting the whole body with ``errors='replace'`` (and
    the resulting replacement-character check) on the common valid-UTF-8 path
    (#5).

    ``body`` may be a bytearray/memoryview when the streamed fetcher returns an
    uncompressed buffer without converting to immutable bytes; ``bytes`` is
    required for the ``.decode()`` calls below, so normalize here exactly once.
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


def _quality_score(
    status: str,
    classification: str,
    text: str | None,
    html: str | None,
    http_status: int | None,
    *,
    content_metrics: dict | None = None,
) -> float:
    if status != "success" or classification != "rendered":
        return 0.0
    # The caller already measured text/html for diagnostics; reuse those exact
    # lengths instead of re-stripping the same two strings (the frameset
    # predicate below is deliberately NOT taken from the metrics dict: the
    # diagnostics tag scan and this substring check disagree on malformed tags).
    if content_metrics is not None:
        text_length = content_metrics["visible_text_length"]
        html_length = content_metrics["html_length"]
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


# Single compiled pattern so _content_metrics counts every tag of interest
# in ONE linear scan of the HTML instead of four separate re.findall/re.search
# passes (#13). Each group is tallied as we walk the string once.
_CONTENT_TAG_RE = re.compile(r"<(a|iframe|frame|frameset|form)\b", re.IGNORECASE)


def _content_metrics(text: str | None, html: str | None) -> dict[str, int | float | bool]:
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
