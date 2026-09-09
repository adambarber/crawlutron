"""Adversarial fix (HIGH #8): classification bypass via HTML comments.

An attacker can inject a block-page signature inside an HTML comment
(e.g. ``<!-- access denied -->``) because ``_html_without_script_and_style``
preserved comments in the ``html_without_script_style`` haystack. Comments are
not user-visible and are trivially attacker-controllable, so they must never
influence classification.
"""

from app.classify import classify_page


class TestClassifyHtmlComments:
    def test_comment_only_signal_is_not_classified(self):
        html = "<html><!-- access denied --></html>"
        # The signature lives ONLY inside an HTML comment (not user-visible).
        # It must NOT force a block classification.
        assert classify_page(html, "clean visible text") == "rendered"

    def test_html_comment_cannot_spoof_any_signature(self):
        # A comment containing every block signature must still yield "rendered"
        # when the visible content is benign.
        comment = (
            "<!-- "
            + " ".join(
                "access denied captcha detected bot challenge javascript required "
                "unsupported browser geolocation blocked waf challenge rate limited "
                "paywall detected login required cookie consent required not found "
                "service unavailable forbidden"
            )
            + " -->"
        )
        html = f"<html>{comment}<body>Welcome to our site</body></html>"
        assert classify_page(html, "Welcome to our site") == "rendered"

    def test_visible_signal_still_classified(self):
        # Regression guard: signals in VISIBLE text must still be detected.
        html = "<html><body>access denied</body></html>"
        assert classify_page(html, "access denied") == "access_denied"
