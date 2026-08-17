from __future__ import annotations

from typing import Any


CLASSIFICATION_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("login_required", ("sign in to continue", "log in to continue", "login required", "sign into your account", "sign in to confirm")),
    ("captcha_detected", ("captcha", "verify you are human", "human verification")),
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
    ("cookie_consent_required", ("accept all cookies", "we use cookies", "cookie consent", "manage cookies")),
    ("javascript_required", ("javascript is required", "enable javascript to view", "requires javascript")),
    ("unsupported_browser", ("browser is not supported", "unsupported browser", "please update your browser")),
    ("geolocation_blocked", ("not available in your location", "unavailable in your region", "location restricted")),
    ("waf_challenge_detected", ("web application firewall", "incident id", "request blocked by web application firewall")),
    ("rate_limited", ("429", "too many requests", "rate limit", "rate-limited")),
    ("paywall_detected", ("subscribe now", "subscription required", "continue reading", "paywall")),
    ("access_denied", ("access denied", "request has been blocked", "forbidden", "security rules", "403 forbidden")),
    ("not_found", ("custom404", "404. the page/url requested", "404 not found", "page not found", "not found")),
    ("site_error", ("runtime error", "server error in '/' application", "application error")),
    ("gone", ("410 gone",)),
)


def classify_page(html: str | None, text: str | None, *, title: str | None = None) -> str:
    classification, _evidence = classify_page_with_evidence(html, text, title=title)
    return classification


def classify_page_with_evidence(html: str | None, text: str | None, *, title: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    visible_haystack = f"{title or ''}\n{text or ''}".lower()
    html_haystack = _html_without_script_and_style(html).lower()
    if not (text or "").strip() and not (title or "").strip() and _html_has_no_user_visible_content(html):
        return "binary_no_text", [
            {
                "classification": "binary_no_text",
                "signature": "empty_visible_content",
                "source": "visible_text",
                "context": "",
            }
        ]
    for classification, signatures in CLASSIFICATION_SIGNATURES:
        for signature in signatures:
            visible_index = visible_haystack.find(signature)
            if visible_index >= 0:
                return classification, [_evidence(classification, signature, "visible_text", visible_haystack, visible_index)]
            html_index = html_haystack.find(signature)
            if html_index >= 0:
                return classification, [_evidence(classification, signature, "html_without_script_style", html_haystack, html_index)]
    return "rendered", []


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


def _html_without_script_and_style(html: str | None) -> str:
    import re

    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html or "", flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)


def _html_has_no_user_visible_content(html: str | None) -> bool:
    normalized = (html or "").strip().lower()
    return normalized in {"", "<html></html>", "<html><head></head><body></body></html>"}
