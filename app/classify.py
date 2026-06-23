from __future__ import annotations


SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
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
    ("rate_limited", ("429", "too many requests", "rate limit", "rate-limited")),
    ("login_required", ("sign in to continue", "log in to continue", "login required", "sign into your account")),
    ("paywall_detected", ("subscribe now", "subscription required", "continue reading", "paywall")),
    ("access_denied", ("access denied", "request has been blocked", "forbidden", "security rules", "403 forbidden")),
    ("not_found", ("custom404", "404. the page/url requested", "404 not found", "page not found", "not found")),
    ("site_error", ("runtime error", "server error in '/' application", "application error")),
    ("gone", ("410 gone",)),
)


def classify_rendered_page(html: str | None, text: str | None, *, title: str | None = None) -> str:
    haystack = f"{title or ''}\n{html or ''}\n{text or ''}".lower()
    if not (text or "").strip() and not (title or "").strip() and _html_has_no_user_visible_content(html):
        return "binary_no_text"
    for classification, signatures in SIGNATURES:
        if any(signature in haystack for signature in signatures):
            return classification
    return "rendered"


def _html_has_no_user_visible_content(html: str | None) -> bool:
    normalized = (html or "").strip().lower()
    return normalized in {"", "<html></html>", "<html><head></head><body></body></html>"}
