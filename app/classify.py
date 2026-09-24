from __future__ import annotations

import re
from typing import Any, Final

from app.models import Classification, as_classification

# Declared as Final[Classification] rather than returned as bare literals. A
# string literal in return position widens to `str`, which is how a function
# annotated `-> tuple[Classification, ...]` ends up handing its caller a plain
# string that merely happens to spell a member. Naming the two values once, with
# the taxonomy type attached, makes the return sites honest and gives the
# membership drift test something to pin.
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


# Structural markers that are effectively unforgeable by accident: a benign
# municipal page never contains Cloudflare/captcha plumbing. Adapted from the
# marker-tuple approach used by maigret (MIT) and cloudscraper (MIT); a single
# marker hit is sufficient evidence.
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

# Classifications whose prose signatures are common English phrases ("too many
# requests", "we value your privacy"). A single phrase match must be
# corroborated by a second independent block signal, otherwise benign pages are
# misclassified and the engine escalates through every strategy for nothing.
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
    stripped_html = _strip_html_noise(html).lower()
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
                return classification, [
                    _evidence(classification, marker, "structural_marker", raw_haystack, index)
                ]

    # Pass 1: prose signatures. Record every distinct match; soft
    # classifications additionally require corroboration (>= 2 distinct
    # matched signatures anywhere on the page) so a stray phrase cannot flip
    # an otherwise-benign result into strategy escalation.
    # Each entry: priority order, classification name, first evidence found.
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
    # The classification travels next to its match (see ``matches`` above) so it
    # keeps its ``Classification`` type instead of being read back off an
    # ``Any`` dict, which would widen a taxonomy member to plain ``str`` at the
    # exact point the declared return has to hold.
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


# Compiled once at module import: the hot path calls _strip_html_noise per page
# on every strategy attempt, so the previous lazy-global compile (which also had
# a benign double-compile race under threads) is replaced with two module-level
# compiled patterns.
_TAGS_RE = re.compile(r"<[^>]+>")


# Cap the input size for the noise-stripping scan. The scanner is linear, so
# this is a resource bound (scan + output size), not a correctness one:
# anything above a few MB is overwhelmingly likely to be a hostile payload.
_MAX_HTML_NOISE_INPUT_BYTES = 4 * 1024 * 1024


def _strip_html_noise(html: str | None) -> str:
    """Remove scripts, styles, and comments via linear string scanning.

    Replaces the previous regex pass (``.*?`` alternation with ``re.DOTALL``),
    which exhibited O(N^2) backtracking on inputs with many unclosed script
    tags (adversarial review H-final-1: 4MB of unclosed tags pinned a worker
    for tens of seconds). ``str.find`` scans each region exactly once, so the
    cost is linear in input size regardless of tag structure.

    Semantics are browser-consistent: an *unclosed* ``<script>``/``<style>``
    or ``<!--`` swallows the remainder of the document (the old regex left
    the unterminated body visible to classification — script code counted
    as page text).
    """
    body = html or ""
    if len(body) > _MAX_HTML_NOISE_INPUT_BYTES:
        body = body[:_MAX_HTML_NOISE_INPUT_BYTES]

    out: list[str] = []
    pos = 0
    lower = body.lower()
    while pos < len(body):
        # Find the earliest noise opener from ``pos``.
        candidates = []
        idx = lower.find("<script", pos)
        if idx != -1:
            candidates.append(idx)
        idx = lower.find("<style", pos)
        if idx != -1:
            candidates.append(idx)
        idx = lower.find("<!--", pos)
        if idx != -1:
            candidates.append(idx)

        if not candidates:
            out.append(body[pos:])
            break

        start = min(candidates)
        out.append(body[pos:start])

        if body.startswith("<!--", start):
            closer = "-->"
        else:
            # ``<script``/``<style`` — locate the closing tag. Match against
            # ``lower`` (the opener scan is case-insensitive; ``body`` may be
            # ``<SCRIPT>``).
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
