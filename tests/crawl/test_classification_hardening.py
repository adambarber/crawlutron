"""Anti-spoofing rules for page classification (adversarial review).

Soft prose signatures (rate_limited, cookie_consent, paywall) require >= 2
independent signals; structural Cloudflare/captcha markers are single-signal.
"""

from app.classify import classify_page


def test_single_soft_phrase_does_not_classify():
    """A benign page mentioning one phrase must stay rendered."""
    assert (
        classify_page(
            "<html><body>Sorry, too many requests. Enjoy the rest of our site.</body></html>",
            None,
        )
        == "rendered"
    )


def test_corroborated_soft_phrases_classify():
    html = "<html><title>429 Too Many Requests</title><body>rate limit exceeded</body></html>"
    assert classify_page(html, None) == "rate_limited"


def test_structural_cloudflare_marker_in_script_is_detected():
    html = "<html><head><script>window._cf_chl_opt = {};</script></head><body>ok</body></html>"
    assert classify_page(html, "ok") == "bot_challenge_detected"


def test_captcha_plumbing_marker():
    assert classify_page('<form><div class="g-recaptcha" data-sitekey="x"></div></form>', "") == (
        "captcha_detected"
    )


def test_access_denied_with_login_prompt_still_login_wall():
    # Two distinct signals corroborate; top-priority (login_required) wins.
    assert classify_page(None, "Access denied - please sign in to continue") == "login_required"


def test_benign_stays_rendered():
    assert classify_page("<html><body>Welcome to our site</body></html>", "Welcome to our site") == (
        "rendered"
    )
