"""Page classification: phrases, structural markers, taxonomy, status resolution and memory bounds."""

from crawlutron.extraction.classify import classify_page, classify_page_with_evidence
from crawlutron.vocabulary.taxonomy import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    CrawlClassification,
)


async def test_single_soft_phrase_does_not_classify():
    """A benign page mentioning one phrase must stay rendered."""
    assert (
        await classify_page(
            "<html><body>Sorry, too many requests. Enjoy the rest of our site.</body></html>",
            None,
        )
        == "rendered"
    )


async def test_corroborated_soft_phrases_classify():
    html = "<html><title>429 Too Many Requests</title><body>rate limit exceeded</body></html>"
    assert await classify_page(html, "rate limit exceeded", title="429 Too Many Requests") == "rate_limited"


async def test_structural_cloudflare_marker_in_script_is_detected():
    html = "<html><head><script>window._cf_chl_opt = {};</script></head><body>ok</body></html>"
    assert await classify_page(html, "ok") == "bot_challenge_detected"


async def test_captcha_plumbing_marker():
    assert await classify_page('<form><div class="g-recaptcha" data-sitekey="x"></div></form>', "") == (
        "captcha_detected"
    )


async def test_access_denied_with_login_prompt_still_login_wall():
    # Two distinct signals corroborate; top-priority (login_required) wins.
    assert await classify_page(None, "Access denied - please sign in to continue") == "login_required"


async def test_benign_stays_rendered():
    assert await classify_page("<html><body>Welcome to our site</body></html>", "Welcome to our site") == (
        "rendered"
    )


async def test_classify_unchanged_after_refactor():
    from crawlutron.extraction.classify import classify_page

    # spoof attempt still neutralized
    assert (
        await classify_page("<!-- access denied --><body>Welcome to our site</body>", "Welcome to our site")
        == "rendered"
    )
    # visible block phrase (hard classification) detected
    assert await classify_page("<body>access denied</body>", "access denied") == "access_denied"


_GROUPINGS = (
    HTTP_BLOCKED_CLASSIFICATIONS,
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
)


class TestClassificationComments:
    def test_html_without_script_style_signal(self):
        classification, evidence = classify_page_with_evidence(
            "<html><!-- access denied --></html>",
            "clean visible text",
        )
        assert classification == "rendered"

    def test_html_visible_signal_detected(self):
        # Visible (non-comment) block signals in the extracted text are detected via the
        # visible-text haystack (classify consumes Extraction.text, not a re-parse of markup).
        classification, evidence = classify_page_with_evidence(
            "<html><body>access denied</body></html>",
            "access denied",
        )
        assert classification == "access_denied"
        assert evidence[0]["source"] == "visible_text"


class TestClassifyHtmlComments:
    async def test_comment_only_signal_is_not_classified(self):
        html = "<html><!-- access denied --></html>"
        # The signature lives ONLY inside an HTML comment (not user-visible). It must NOT force a block
        # classification.
        assert await classify_page(html, "clean visible text") == "rendered"

    async def test_html_comment_cannot_spoof_any_signature(self):
        # A comment containing every block signature must still yield "rendered" when the visible content is
        # benign.
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
        assert await classify_page(html, "Welcome to our site") == "rendered"

    async def test_visible_signal_still_classified(self):
        # Regression guard: signals in VISIBLE text must still be detected.
        html = "<html><body>access denied</body></html>"
        assert await classify_page(html, "access denied") == "access_denied"


def test_wire_classification_literal_matches_the_enum():
    """``Classification`` IS the enum now -- one declaration, no second copy.

    The historical shape (a hand-maintained Literal that could drift) died in
    the unification: the alias is the class itself, so drift is unrepresentable.
    """
    from crawlutron.models.taxonomy import Classification

    assert Classification is CrawlClassification
    assert {member.value for member in Classification} == {m.value for m in CrawlClassification}
