from __future__ import annotations


SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("captcha_detected", ("captcha", "verify you are human", "human verification")),
    ("rate_limited", ("429", "too many requests", "rate limit", "rate-limited")),
    ("login_required", ("sign in to continue", "log in to continue", "login required", "sign into your account")),
    ("paywall_detected", ("subscribe now", "subscription required", "continue reading", "paywall")),
    ("access_denied", ("access denied", "request has been blocked", "forbidden", "security rules")),
)


def classify_rendered_page(html: str | None, text: str | None) -> str:
    haystack = f"{html or ''}\n{text or ''}".lower()
    for classification, signatures in SIGNATURES:
        if any(signature in haystack for signature in signatures):
            return classification
    return "rendered"
