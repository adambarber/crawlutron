from __future__ import annotations

from typing import NamedTuple

from crawlutron.models.classification_evidence import ClassificationEvidenceRecord
from crawlutron.models.taxonomy import Classification

# Named Classification members, not bare literals: a literal in return position
# widens to ``str`` and hands the caller a plain string that merely spells a member.
_BINARY_NO_TEXT = Classification.binary_no_text
_RENDERED = Classification.rendered

CLASSIFICATION_SIGNATURES: tuple[tuple[Classification, tuple[str, ...]], ...] = (
    (
        Classification.login_required,
        (
            "sign in to continue",
            "log in to continue",
            "login required",
            "sign into your account",
            "sign in to confirm",
        ),
    ),
    (
        Classification.captcha_detected,
        (
            "verify you are human",
            "human verification",
            "complete the captcha",
            "captcha challenge",
            "prove you are human",
            "i'm not a robot",
        ),
    ),
    (
        Classification.bot_challenge_detected,
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
        Classification.cookie_consent_required,
        (
            "this site uses cookies to",
            "by continuing you agree",
            "we value your privacy",
            "cookie settings",
            "reject all",
            "manage preferences",
        ),
    ),
    (
        Classification.javascript_required,
        ("javascript is required", "enable javascript to view", "requires javascript"),
    ),
    (
        Classification.unsupported_browser,
        ("browser is not supported", "unsupported browser", "please update your browser"),
    ),
    (
        Classification.geolocation_blocked,
        ("not available in your location", "unavailable in your region", "location restricted"),
    ),
    (
        Classification.waf_challenge_detected,
        ("web application firewall", "incident id", "request blocked by web application firewall"),
    ),
    (
        Classification.rate_limited,
        ("too many requests", "rate limit exceeded", "rate-limited", "throttled", "quota exceeded"),
    ),
    (
        Classification.paywall_detected,
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
        Classification.access_denied,
        ("access denied", "request has been blocked", "security rules", "403 forbidden"),
    ),
    (
        Classification.not_found,
        ("custom404", "404. the page/url requested", "404 not found", "page not found"),
    ),
    (Classification.site_error, ("runtime error", "server error in '/' application", "application error")),
    (Classification.gone, ("410 gone",)),
)


def classify_page(html: str | None, text: str | None, *, title: str | None = None) -> Classification:
    classification, _evidence = classify_page_with_evidence(html, text, title=title)
    return classification


# Structural markers that are unforgeable by accident (legitimate pages do not
# carry Cloudflare/captcha plumbing); one hit suffices. NOTICE carries the MIT lines.
STRONG_CLASSIFICATION_MARKERS: tuple[tuple[Classification, tuple[str, ...]], ...] = (
    (
        Classification.bot_challenge_detected,
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
        Classification.captcha_detected,
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
        Classification.rate_limited,
        Classification.cookie_consent_required,
        Classification.paywall_detected,
    }
)


def classify_page_with_evidence(
    html: str | None, text: str | None, *, title: str | None = None
) -> tuple[Classification, list[ClassificationEvidenceRecord]]:
    visible_haystack = f"{title or ''}\n{text or ''}".lower()
    html_haystack = (text or "").lower()
    if not (text or "").strip() and not (title or "").strip() and _html_has_no_user_visible_content(html):
        return _BINARY_NO_TEXT, [
            ClassificationEvidenceRecord(
                classification=_BINARY_NO_TEXT,
                signature="empty_visible_content",
                source="visible_text",
                context="",
            )
        ]

    # Pass 0: structural markers win immediately (one hit suffices). Scan the RAW
    # html too -- challenge plumbing (_cf_chl_opt) lives inside stripped <script>s.
    raw_haystack = (html or "").lower()
    structural = _structural_marker_match(raw_haystack, html_haystack)
    if structural is not None:
        return structural

    matches = _prose_signature_matches(visible_haystack, html_haystack)
    return _verdict_from_matches(matches)


def _structural_marker_match(
    raw_haystack: str, html_haystack: str
) -> tuple[Classification, list[ClassificationEvidenceRecord]] | None:
    """Pass 0: the first structural marker hit wins, with its one evidence row."""
    for classification, markers in STRONG_CLASSIFICATION_MARKERS:
        for marker in markers:
            index = raw_haystack.find(marker)
            if index < 0:
                index = html_haystack.find(marker)
            if index >= 0:
                return classification, [
                    _evidence(classification, marker, "structural_marker", raw_haystack, index)
                ]
    return None


class _ProseMatch(NamedTuple):
    """One prose-signature hit, with its pass priority and match position."""

    priority: int
    classification: Classification
    signature: str
    source: str
    haystack: str
    offset: int


def _prose_signature_matches(visible_haystack: str, html_haystack: str) -> list[_ProseMatch]:
    """Pass 1: every distinct prose-signature match; earliest source wins per
    signature, and each match carries its priority for the verdict pass.
    """
    matches: list[_ProseMatch] = []
    for priority, (classification, signatures) in enumerate(CLASSIFICATION_SIGNATURES):
        for signature in signatures:
            for source, haystack in (
                ("visible_text", visible_haystack),
                ("html_without_script_style", html_haystack),
            ):
                index = haystack.find(signature)
                if index >= 0:
                    matches.append(
                        _ProseMatch(
                            priority=priority,
                            classification=classification,
                            signature=signature,
                            source=source,
                            haystack=haystack,
                            offset=index,
                        )
                    )
                    break  # earliest source is enough for this signature
    return matches


def _verdict_from_matches(
    matches: list[_ProseMatch],
) -> tuple[Classification, list[ClassificationEvidenceRecord]]:
    """Reduce the prose matches: none -> rendered; soft signals need corroboration."""
    if not matches:
        return _RENDERED, []

    distinct_classifications = {match.classification for match in matches}
    best_priority = min(match.priority for match in matches)
    chosen = min(
        (match for match in matches if match.priority == best_priority),
        key=lambda match: match.offset,
    )
    # The classification rides its own record, so it keeps its ``Classification``
    # type instead of widening to ``str`` off an ``Any`` dict at the return.
    classification = chosen.classification

    # Corroboration: >= 2 independent signals anywhere on the page (either two
    # different block classifications, or two distinct phrases of the same one).
    corroborated = len(distinct_classifications) >= 2 or len(matches) >= 2
    if classification in SOFT_SINGLE_SIGNAL_CLASSIFICATIONS and not corroborated:
        return _RENDERED, []

    return classification, [
        _evidence(
            classification,
            chosen.signature,
            chosen.source,
            chosen.haystack,
            chosen.offset,
        )
    ]


def _evidence(
    classification: Classification, signature: str, source: str, haystack: str, index: int
) -> ClassificationEvidenceRecord:
    start = max(0, index - 80)
    end = min(len(haystack), index + len(signature) + 120)
    context = " ".join(haystack[start:end].split())
    return ClassificationEvidenceRecord(
        classification=classification,
        signature=signature,
        source=source,
        context=context,
    )


def _html_has_no_user_visible_content(html: str | None) -> bool:
    normalized = (html or "").strip().lower()
    return normalized in {"", "<html></html>", "<html><head></head><body></body></html>"}
