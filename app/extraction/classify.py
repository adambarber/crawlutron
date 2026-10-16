from __future__ import annotations

import re
from typing import Any, Final

from app.models.taxonomy import Classification, as_classification
from app.safety.bounded_io import truncate_html_input

# Final[Classification], not bare literals: a literal in return position widens
# to ``str`` and hands the caller a plain string that merely spells a member.
_BINARY_NO_TEXT: Final[Classification] = "binary_no_text"
_RENDERED: Final[Classification] = "rendered"

CLASSIFICATION_SIGNATURES: tuple[tuple[Classification, tuple[str, ...]], ...] = (
    (
        "login_required",
        (
            "sign in to continue",
            "log in to continue",
            "login required",
            "sign into your account",
            "sign in to confirm",
        ),
    ),
    (
        "captcha_detected",
        (
            "verify you are human",
            "human verification",
            "complete the captcha",
            "captcha challenge",
            "prove you are human",
            "security check",
            "i'm not a robot",
        ),
    ),
    (
        "bot_challenge_detected",
        (
            "just a moment",
            "attention required",
            "checking your browser",
            "enable javascript and cookies",
            "cf-browser-verification",
            "cf-chl",
            "cloudflare ray id",
        ),
    ),
    (
        "cookie_consent_required",
        (
            "this site uses cookies to",
            "by continuing you agree",
            "we value your privacy",
            "cookie settings",
            "reject all",
            "manage preferences",
        ),
    ),
    ("javascript_required", ("javascript is required", "enable javascript to view", "requires javascript")),
    (
        "unsupported_browser",
        ("browser is not supported", "unsupported browser", "please update your browser"),
    ),
    (
        "geolocation_blocked",
        ("not available in your location", "unavailable in your region", "location restricted"),
    ),
    (
        "waf_challenge_detected",
        ("web application firewall", "incident id", "request blocked by web application firewall"),
    ),
    (
        "rate_limited",
        ("too many requests", "rate limit exceeded", "rate-limited", "throttled", "quota exceeded"),
    ),
    (
        "paywall_detected",
        (
            "subscription required to",
            "paywall",
            "metered paywall",
            "premium subscription",
            "unlock this article",
            "subscriber only",
            "members only",
        ),
    ),
    (
        "access_denied",
        ("access denied", "request has been blocked", "forbidden", "security rules", "403 forbidden"),
    ),
    (
        "not_found",
        ("custom404", "404. the page/url requested", "404 not found", "page not found", "not found"),
    ),
    ("site_error", ("runtime error", "server error in '/' application", "application error")),
    ("gone", ("410 gone",)),
)


def classify_page(html: str | None, text: str | None, *, title: str | None = None) -> Classification:
    classification, _evidence = classify_page_with_evidence(html, text, title=title)
    return classification


# Structural markers that are unforgeable by accident (no municipal page carries
# Cloudflare/captcha plumbing); one hit suffices. NOTICE carries the MIT lines.
STRONG_CLASSIFICATION_MARKERS: tuple[tuple[Classification, tuple[str, ...]], ...] = (
    (
        "bot_challenge_detected",
        (
            "_cf_chl_opt",
            "cf_chl_prog",
            "jschl-answer",
            "jschl_vc",
            "challenges.cloudflare.com",
            "/cdn-cgi/challenge-platform/",
            "cf-mitigated",
            "checking your browser before you access",
            "attention required! | cloudflare",
        ),
    ),
    (
        "captcha_detected",
        (
            "g-recaptcha",
            "grecaptcha.render",
            "hcaptcha",
            "cf-turnstile",
            "data-sitekey",
        ),
    ),
)

# Prose signatures that are common English phrases ("too many requests", "we
# value our privacy"): a single hit must be corroborated or benign pages flip.
SOFT_SINGLE_SIGNAL_CLASSIFICATIONS = frozenset(
    {
        "rate_limited",
        "cookie_consent_required",
        "paywall_detected",
    }
)


def classify_page_with_evidence(
    html: str | None, text: str | None, *, title: str | None = None
) -> tuple[Classification, list[dict[str, Any]]]:
    stripped_html = strip_html_noise(html).lower()
    visible_haystack = f"{title or ''}\n{text or ''}".lower()
    html_haystack = stripped_html
    if not (text or "").strip() and not (title or "").strip() and _html_has_no_user_visible_content(html):
        return _BINARY_NO_TEXT, [
            {
                "classification": "binary_no_text",
                "signature": "empty_visible_content",
                "source": "visible_text",
                "context": "",
            }
        ]

    # Pass 0: structural markers win immediately (one hit suffices). Scan the RAW
    # html too -- challenge plumbing (_cf_chl_opt) lives inside stripped <script>s.
    raw_haystack = (html or "").lower()
    for classification, markers in STRONG_CLASSIFICATION_MARKERS:
        for marker in markers:
            index = raw_haystack.find(marker)
            if index < 0:
                index = html_haystack.find(marker)
            if index >= 0:
                return classification, [
                    _evidence(classification, marker, "structural_marker", raw_haystack, index)
                ]

    # Pass 1: prose signatures, every distinct match recorded; soft ones also
    # need corroboration (>= 2 matches) so a stray phrase cannot escalate.
    matches: list[tuple[Classification, dict[str, Any]]] = []
    for priority, (classification, signatures) in enumerate(CLASSIFICATION_SIGNATURES):
        for signature in signatures:
            for source, haystack in (
                ("visible_text", visible_haystack),
                ("html_without_script_style", html_haystack),
            ):
                index = haystack.find(signature)
                if index >= 0:
                    matches.append(
                        (
                            classification,
                            {
                                "priority": priority,
                                "classification": classification,
                                "signature": signature,
                                "source": source,
                                "haystack": haystack,
                                "index": index,
                            },
                        )
                    )
                    break  # earliest source is enough for this signature
    if not matches:
        return _RENDERED, []

    distinct_classifications = {name for name, _match in matches}
    best_priority = min(match["priority"] for _name, match in matches)
    chosen = min(
        (match for _name, match in matches if match["priority"] == best_priority),
        key=lambda match: match["index"],
    )
    # The classification travels with its match so it keeps its ``Classification``
    # type instead of widening to ``str`` off an ``Any`` dict at the return.
    classification = as_classification(next(name for name, match in matches if match is chosen))

    # Corroboration: >= 2 independent signals anywhere on the page (either two
    # different block classifications, or two distinct phrases of the same one).
    corroborated = len(distinct_classifications) >= 2 or len(matches) >= 2
    if classification in SOFT_SINGLE_SIGNAL_CLASSIFICATIONS and not corroborated:
        return _RENDERED, []

    return classification, [
        _evidence(
            classification,
            chosen["signature"],
            chosen["source"],
            chosen["haystack"],
            chosen["index"],
        )
    ]


def _evidence(
    classification: Classification, signature: str, source: str, haystack: str, index: int
) -> dict[str, str]:
    start = max(0, index - 80)
    end = min(len(haystack), index + len(signature) + 120)
    context = " ".join(haystack[start:end].split())
    return {
        "classification": classification,
        "signature": signature,
        "source": source,
        "context": context,
    }


# Compiled at import: strip_html_noise runs per page on every strategy attempt,
# and the lazy-global compile also carried a benign double-compile race under threads.
_TAGS_RE = re.compile(r"<[^>]+>")


# Cap the scan input: linear, so the bound covers resources, not correctness.


#: Openers whose closers are stripped before classification: a declaration.
_NOISE_OPENERS: Final = ("<script", "<style", "<!--")


def strip_html_noise(html: str | None) -> str:
    """Remove script/style/comment regions via linear string scanning (str.find), not a
    backtracking regex -- and browser-consistent: an *unclosed* ``<script>``/``<style>``/``<!--``
    swallows the rest of the document, so code is never counted as page text.
    """
    body = truncate_html_input(html)

    out: list[str] = []
    pos = 0
    lower = body.lower()
    while pos < len(body):
        # The openers are a set, not a branch per tag: each branch added two lines
        # and a tag to remember; the list is what the scan knows about noise.
        candidates = [idx for idx in (lower.find(opener, pos) for opener in _NOISE_OPENERS) if idx != -1]

        if not candidates:
            out.append(body[pos:])
            break

        start = min(candidates)
        out.append(body[pos:start])

        if body.startswith("<!--", start):
            closer = "-->"
        else:
            # Match against ``lower``: the opener scan is case-insensitive, and
            # body may appear as ``<SCRIPT>``.
            tag = "script" if lower.startswith("<script", start) else "style"
            # Word-boundary guard (regex ``\b`` parity): ``<scriptable`` is a
            # run of text, not an opening tag.
            after = start + 1 + len(tag)
            if after < len(body) and (body[after].isalnum() or body[after] == "_"):
                # Not a real opener (``<scriptable``): keep the token verbatim.
                out.append(body[start : start + 2])
                pos = start + 2
                continue
            closer = f"</{tag}"

        end = lower.find(closer, start)
        if end == -1:
            # Unclosed block: swallow the remainder and stop.
            pos = len(body)
            break
        # Consume through the closing tag's ``>`` (handles attributes/space).
        gt = body.find(">", end)
        pos = len(body) if gt == -1 else gt + 1

    without_noise = "".join(out)
    return _TAGS_RE.sub(" ", without_noise)


def _html_has_no_user_visible_content(html: str | None) -> bool:
    normalized = (html or "").strip().lower()
    return normalized in {"", "<html></html>", "<html><head></head><body></body></html>"}
