"""Page classification: phrases, structural markers, taxonomy, status resolution and memory bounds."""

from app.extraction.classify import classify_page, classify_page_with_evidence
from app.vocabulary.taxonomy import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    CrawlClassification,
)
from tests._timing_harnesses import cpu_seconds


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


def _strip(html):
    from app.extraction.classify import strip_html_noise

    return strip_html_noise(html)


def test_strips_scripts_styles_comments_in_one_pass():
    html = (
        "<html><head><script>var a=1;</script>"
        "<style>.x{}</style><!-- comment --><body><p>visible text</p></body></html>"
    )
    out = _strip(html)
    assert "var a=1" not in out
    assert ".x{}" not in out
    assert "comment" not in out
    assert "visible text" in out


def test_case_insensitive_tags():
    out = _strip("<SCRIPT>x()</SCRIPT><p>keep me</p>")
    assert "x()" not in out and "keep me" in out


def test_classify_unchanged_after_refactor():
    from app.extraction.classify import classify_page

    # spoof attempt still neutralized
    assert (
        classify_page("<!-- access denied --><body>Welcome to our site</body>", "Welcome to our site")
        == "rendered"
    )
    # visible block phrase (hard classification) detected
    assert classify_page("<body>access denied</body>", "access denied") == "access_denied"


def test_strip_is_linear_under_pathological_unclosed_script_flood():
    """Unclosed <script> tags must not trigger quadratic regex backtracking.

    N unclosed tags made the noise regex O(N^2); measured in CPU seconds (wall
    clock lies) and sized 1MB->4MB so the linear growth term dominates the noise.
    """
    from app.extraction.classify import strip_html_noise

    def cpu_for(n):
        payload = ("<script>" + "a" * 500) * n
        return cpu_seconds(lambda: strip_html_noise(payload))

    t_small = cpu_for(2_000)
    t_big = cpu_for(8_000)
    assert t_big < t_small * 7, (
        f"super-linear blowup: {t_small:.6f}s -> {t_big:.6f}s (ratio {t_big / t_small:.1f}x for 4x input)"
    )
    # Absolute ceiling, set from the measured ~2ns/byte: a linear pass over 4MB costs ~8ms, while the
    # quadratic this guards against costs minutes.
    assert t_big < 0.05, f"linear scan too slow: {t_big:.6f}s of CPU for ~4MB"


def test_strip_is_linear_when_noise_regions_are_all_closed():
    """Many CLOSED regions must not rescan for the absent openers (#440).

    Each consumed region used to pay two find() scans of the whole remaining
    body for the openers that were not there -- O(k*n) in k closed regions.
    """
    from app.extraction.classify import strip_html_noise

    one_region = "<script>" + "b" * 100 + "</script>"
    small = cpu_seconds(lambda: strip_html_noise(one_region * 1_000))
    big = cpu_seconds(lambda: strip_html_noise(one_region * 4_000))
    assert big < small * 7, f"closed-region flood is super-linear: {small:.6f}s -> {big:.6f}s"
    assert big < 1.0, f"{big:.6f}s of CPU for ~468KB is not a linear scan"


def test_strip_is_linear_when_text_has_many_lt_but_no_gt():
    """A page of bare ``<`` tokens with no ``>`` must not backtrack (#440).

    The tail tag-strip ``<[^>]+>`` re-scanned to end-of-string at every ``<``
    on such text -- the same O(k*n) class, hiding behind the scanner fix.
    """
    from app.extraction.classify import strip_html_noise

    big = cpu_seconds(lambda: strip_html_noise("<b" * 100_000))
    assert big < 1.0, f"{big:.6f}s of CPU for 200KB of bare '<' tokens is not a linear strip"


def test_strip_unclosed_script_neutralizes_to_end_and_keeps_preceding_text():
    """An unclosed <script> swallows the remainder (browser-consistent)."""
    out = _strip("<p>visible</p><script>alert(1);<p>hidden tail</p>")
    assert "visible" in out
    assert "alert(1)" not in out
    assert "hidden tail" not in out


def test_strip_comment_and_style_parities():
    out = _strip("<p>keep</p><!-- secret <p>gone</p>")
    assert "keep" in out and "secret" not in out and "gone" not in out
    out2 = _strip("<p>keep2</p><style>.a{}<p>gone2</p>")
    assert "keep2" in out2 and ".a{}" not in out2 and "gone2" not in out2
    out3 = _strip("<script>a</script><style>b</style><!--c--><p>tail</p>")
    assert out3.strip() == "tail", repr(out3)


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
        # Visible (non-comment) block signals in the HTML body must still be detected via the
        # html_without_script_style haystack.
        classification, evidence = classify_page_with_evidence(
            "<html><body>access denied</body></html>",
            None,
        )
        assert classification == "access_denied"
        assert evidence[0]["source"] == "html_without_script_style"


class TestClassifyHtmlComments:
    def test_comment_only_signal_is_not_classified(self):
        html = "<html><!-- access denied --></html>"
        # The signature lives ONLY inside an HTML comment (not user-visible). It must NOT force a block
        # classification.
        assert classify_page(html, "clean visible text") == "rendered"

    def test_html_comment_cannot_spoof_any_signature(self):
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
        assert classify_page(html, "Welcome to our site") == "rendered"

    def test_visible_signal_still_classified(self):
        # Regression guard: signals in VISIBLE text must still be detected.
        html = "<html><body>access denied</body></html>"
        assert classify_page(html, "access denied") == "access_denied"


def test_wire_classification_literal_matches_the_enum():
    """The ``Classification`` Literal is a hand-maintained second copy of the enum.

    A member added to the enum but forgotten in the Literal passes CI -- the
    detector checks call-site literals -- which produced the original defect.
    """
    import typing

    from app.models.taxonomy import Classification

    args = typing.get_args(Classification)
    enum_values = {m.value for m in CrawlClassification}
    literal_values = set(args)

    assert literal_values == enum_values, (
        f"Classification Literal drifted from CrawlClassification: "
        f"missing from Literal={sorted(enum_values - literal_values)} "
        f"stale in Literal={sorted(literal_values - enum_values)}"
    )
