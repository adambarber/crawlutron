from __future__ import annotations

import re
from typing import Any

CLASSIFICATION_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("login_required", ("sign in to continue", "log in to continue", "login required", "sign into your account", "sign in to confirm")),
    ("captcha_detected", ("verify you are human", "human verification", "complete the captcha", "captcha challenge", "prove you are human", "security check", "i'm not a robot")),
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
    ("cookie_consent_required", ("this site uses cookies to", "by continuing you agree", "we value your privacy", "cookie settings", "reject all", "manage preferences")),
    ("javascript_required", ("javascript is required", "enable javascript to view", "requires javascript")),
    ("unsupported_browser", ("browser is not supported", "unsupported browser", "please update your browser")),
    ("geolocation_blocked", ("not available in your location", "unavailable in your region", "location restricted")),
    ("waf_challenge_detected", ("web application firewall", "incident id", "request blocked by web application firewall")),
    ("rate_limited", ("too many requests", "rate limit exceeded", "rate-limited", "throttled", "quota exceeded")),
    ("paywall_detected", ("subscription required to", "paywall", "metered paywall", "premium subscription", "unlock this article", "subscriber only", "members only")),
    ("access_denied", ("access denied", "request has been blocked", "forbidden", "security rules", "403 forbidden")),
    ("not_found", ("custom404", "404. the page/url requested", "404 not found", "page not found", "not found")),
    ("site_error", ("runtime error", "server error in '/' application", "application error")),
    ("gone", ("410 gone",)),
)


def classify_page(html: str | None, text: str | None, *, title: str | None = None) -> str:
    classification, _evidence = classify_page_with_evidence(html, text, title=title)
    return classification


# Structural markers that are effectively unforgeable by accident: a benign
# municipal page never contains Cloudflare/captcha plumbing. Adapted from the
# marker-tuple approach used by maigret (MIT) and cloudscraper (MIT); a single
# marker hit is sufficient evidence.
STRONG_CLASSIFICATION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bot_challenge_detected", (
        "_cf_chl_opt",
        "cf_chl_prog",
        "jschl-answer",
        "jschl_vc",
        "challenges.cloudflare.com",
        "/cdn-cgi/challenge-platform/",
        "cf-mitigated",
        "checking your browser before you access",
        "attention required! | cloudflare",
    )),
    ("captcha_detected", (
        "g-recaptcha",
        "grecaptcha.render",
        "hcaptcha",
        "cf-turnstile",
        "data-sitekey",
    )),
)

# Classifications whose prose signatures are common English phrases ("too many
# requests", "we value your privacy"). A single phrase match must be
# corroborated by a second independent block signal, otherwise benign pages are
# misclassified and the engine escalates through every strategy for nothing.
SOFT_SINGLE_SIGNAL_CLASSIFICATIONS = frozenset({
    "rate_limited",
    "cookie_consent_required",
    "paywall_detected",
})


def classify_page_with_evidence(html: str | None, text: str | None, *, title: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    stripped_html = _strip_html_noise(html).lower()
    visible_haystack = f"{title or ''}\n{text or ''}".lower()
    html_haystack = stripped_html
    if not (text or "").strip() and not (title or "").strip() and _html_has_no_user_visible_content(html):
        return "binary_no_text", [
            {
                "classification": "binary_no_text",
                "signature": "empty_visible_content",
                "source": "visible_text",
                "context": "",
            }
        ]

    # Pass 0: structural markers win immediately (single hit suffices).
    # NOTE: scan the RAW html too -- challenge plumbing (_cf_chl_opt etc.)
    # lives inside <script> tags which html_haystack strips.
    raw_haystack = (html or "").lower()
    for classification, markers in STRONG_CLASSIFICATION_MARKERS:
        for marker in markers:
            index = raw_haystack.find(marker)
            if index < 0:
                index = html_haystack.find(marker)
            if index >= 0:
                return classification, [_evidence(classification, marker, "structural_marker", raw_haystack, index)]

    # Pass 1: prose signatures. Record every distinct match; soft
    # classifications additionally require corroboration (>= 2 distinct
    # matched signatures anywhere on the page) so a stray phrase cannot flip
    # an otherwise-benign result into strategy escalation.
    # Each entry: priority order, classification name, first evidence found.
    matches: list[dict[str, Any]] = []
    for priority, (classification, signatures) in enumerate(CLASSIFICATION_SIGNATURES):
        for signature in signatures:
            for source, haystack in (("visible_text", visible_haystack), ("html_without_script_style", html_haystack)):
                index = haystack.find(signature)
                if index >= 0:
                    matches.append({
                        "priority": priority,
                        "classification": classification,
                        "signature": signature,
                        "source": source,
                        "haystack": haystack,
                        "index": index,
                    })
                    break  # earliest source is enough for this signature
    if not matches:
        return "rendered", []

    distinct_classifications = {m["classification"] for m in matches}
    best_priority = min(m["priority"] for m in matches)
    chosen = min((m for m in matches if m["priority"] == best_priority),
                 key=lambda m: m["index"])
    classification = chosen["classification"]

    # Corroboration: >= 2 independent signals anywhere on the page (either two
    # different block classifications, or two distinct phrases of the same one).
    corroborated = len(distinct_classifications) >= 2 or len(matches) >= 2
    if classification in SOFT_SINGLE_SIGNAL_CLASSIFICATIONS and not corroborated:
        return "rendered", []

    return classification, [_evidence(
        classification,
        chosen["signature"],
        chosen["source"],
        chosen["haystack"],
        chosen["index"],
    )]


def _evidence(classification: str, signature: str, source: str, haystack: str, index: int) -> dict[str, str]:
    start = max(0, index - 80)
    end = min(len(haystack), index + len(signature) + 120)
    context = " ".join(haystack[start:end].split())
    return {
        "classification": classification,
        "signature": signature,
        "source": source,
        "context": context,
    }


# Compiled once at module import: the hot path calls _strip_html_noise per page
# on every strategy attempt, so the previous lazy-global compile (which also had
# a benign double-compile race under threads) is replaced with two module-level
# compiled patterns.
_TAGS_RE = re.compile(r"<[^>]+>")
_HTML_NOISE_RE = re.compile(
    r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>|<!--[\s\S]*?-->",
    re.IGNORECASE | re.DOTALL,
)


# Cap the input size passed to the noise-stripping regex. The pattern uses
# ``.*?`` with ``re.DOTALL`` and alternation across three branches, which can
# exhibit quadratic backtracking on inputs with many unclosed script/style
# tags. Anything above a few MB is overwhelmingly likely to be a hostile
# payload; truncate before stripping so we never feed the regex a
# pathological input (adversarial review F-html-noise-re-quadratic).
_MAX_HTML_NOISE_INPUT_BYTES = 4 * 1024 * 1024


def _strip_html_noise(html: str | None) -> str:
    """Remove scripts, styles, and comments in a SINGLE regex pass.

    Replaces the previous 4 sequential re.sub calls, each of which produced a
    full copy of the HTML (~6x page size in intermediate strings). Truncates
    ``html`` to ``_MAX_HTML_NOISE_INPUT_BYTES`` before the regex pass so a
    pathological input (many unclosed tags triggering alternation
    backtracking) cannot pin a worker (adversarial F-html-noise-re-quadratic).
    """
    body = html or ""
    if len(body) > _MAX_HTML_NOISE_INPUT_BYTES:
        body = body[:_MAX_HTML_NOISE_INPUT_BYTES]
    without_noise = _HTML_NOISE_RE.sub(" ", body)
    return _TAGS_RE.sub(" ", without_noise)


def _html_has_no_user_visible_content(html: str | None) -> bool:
    normalized = (html or "").strip().lower()
    return normalized in {"", "<html></html>", "<html><head></head><body></body></html>"}
