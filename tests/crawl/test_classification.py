"""Page classification: phrases, structural markers, taxonomy, status resolution and memory bounds."""

import inspect

import pytest
from _testkit import *

from app import constants as app_constants
from app.api_models import CrawlClassification
from app.classify import CLASSIFICATION_SIGNATURES, STRONG_CLASSIFICATION_MARKERS, classify_page
from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    CLASSIFICATION_STATUS_BY_LABEL,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    status_for_classification,
)
from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies import http as http_mod
from app.crawl.strategies.browser import BrowserRenderer


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


# Labels that are not in the taxonomy at all: this is exactly where the two
# historical implementations disagreed.
UNRECOGNIZED_LABELS = ("totally_new_label", "some_future_block", "")


def _classifiable_labels() -> list[str]:
    """Every label the classifiers can emit, plus the cross-module freshness one."""
    labels = {"rendered", "not_modified"}
    labels.update(signature[0] for signature in CLASSIFICATION_SIGNATURES)
    labels.update(marker[0] for marker in STRONG_CLASSIFICATION_MARKERS)
    return sorted(labels)


@pytest.fixture
def browser_resolver():
    """The resolver the browser path actually uses.

    Both paths now share one function, so this is the same object the HTTP path
    calls -- which is the point: there is nothing left to disagree with.
    """
    return status_for_classification


def test_the_status_resolver_is_defined_once_not_per_strategy():
    """Drift guard: no strategy module may reintroduce its own resolver.

    The historical defect was two independent if-chains. Both call sites now
    import ``status_for_classification`` from ``app.constants``, and a new
    per-module copy here is exactly the regression this pins.
    """
    for module in (http_mod, browser_mod):
        assert not hasattr(module, "_status_for_classification"), (
            f"{module.__name__} reintroduced a per-module status resolver"
        )
    assert not hasattr(BrowserRenderer, "_status_for_classification"), (
        "BrowserRenderer reintroduced a per-instance status resolver"
    )

    # The call sites must route through the shared name, not a local copy.
    for module in (http_mod, browser_mod):
        source = inspect.getsource(module)
        assert "status_for_classification(" in source, (
            f"{module.__name__} no longer resolves classifications through the shared app.constants table"
        )


def test_every_classifiable_label_is_an_explicit_table_entry():
    """Drift guard: no classifier output may fall through to the default.

    Adding a signature to ``app.classify`` without giving it a status is the
    regression this pins; it is what let the two if-chains drift apart in the
    first place (an unlisted label silently took whichever default the local
    copy happened to use).
    """
    table = app_constants.CLASSIFICATION_STATUS_BY_LABEL
    missing = [label for label in _classifiable_labels() if label not in table]
    assert not missing, f"classifications with no explicit status entry: {missing}"


def test_every_classifiable_label_resolves_through_the_table(browser_resolver):
    """Every label the classifiers can emit takes its status from the one table.

    This is the direct migration of the pre-consolidation check that the HTTP and
    browser paths agree on every label: with one shared resolver the agreement
    is by construction, so the assertion is now that the resolver both paths use
    is the table itself.
    """
    labels = _classifiable_labels() + sorted(FAILED_CLASSIFICATIONS)
    labels += sorted(HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS)
    labels += list(UNRECOGNIZED_LABELS)
    for label in labels:
        expected = app_constants.CLASSIFICATION_STATUS_BY_LABEL.get(
            label, app_constants.UNKNOWN_CLASSIFICATION_STATUS
        )
        assert status_for_classification(label) == expected
        assert browser_resolver(label) == expected


def test_unknown_classification_has_one_documented_default(browser_resolver):
    """A label nobody knows about must resolve to the documented default."""
    for label in UNRECOGNIZED_LABELS:
        status = status_for_classification(label)
        assert status == browser_resolver(label) == app_constants.UNKNOWN_CLASSIFICATION_STATUS
        # The safe default may never be "success": an unrecognized label has
        # not produced verified content.
        assert status != "success"


def test_known_group_statuses_are_unchanged(browser_resolver):
    """Inverse case: the working paths must keep their exact statuses."""
    assert status_for_classification("rendered") == "success"
    assert browser_resolver("rendered") == "success"
    # Conditional freshness is a success: the caller's validators proved it.
    assert status_for_classification("not_modified") == "success"
    assert app_constants.status_for_classification("not_modified") == "success"
    for classification in BROWSER_BLOCKED_CLASSIFICATIONS:
        assert browser_resolver(classification) == "blocked"
    for classification in HTTP_BLOCKED_CLASSIFICATIONS:
        assert status_for_classification(classification) == "blocked"
    for classification in FAILED_CLASSIFICATIONS:
        assert status_for_classification(classification) == "failed"
        assert browser_resolver(classification) == "failed"


class TestStatusForClassification:
    def test_http_status_not_found(self):
        from app.constants import status_for_classification

        status = status_for_classification("not_found")
        assert status == "failed"

    def test_http_status_site_error(self):
        from app.constants import status_for_classification

        status = status_for_classification("site_error")
        assert status == "failed"


"""Single-pass haystack construction: same results, fewer string copies."""


def _strip(html):
    from app.classify import _strip_html_noise

    return _strip_html_noise(html)


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
    from app.classify import classify_page

    # spoof attempt still neutralized
    assert (
        classify_page("<!-- access denied --><body>Welcome to our site</body>", "Welcome to our site")
        == "rendered"
    )
    # visible block phrase (hard classification) detected
    assert classify_page("<body>access denied</body>", "access denied") == "access_denied"


def test_strip_is_linear_under_pathological_unclosed_script_flood():
    """Unclosed <script> tags must not trigger quadratic regex backtracking.

    A page with N unclosed <script> tags made _HTML_NOISE_RE cost O(N^2):
    each tag anchor re-scanned to end-of-string for a closing tag that never
    comes (adversarial review H-final-1). The replacement scanner must stay
    linear: doubling the input must roughly double, not quadruple, the time.
    """
    import time

    from app.classify import _strip_html_noise

    def timed(n):
        payload = ("<script>" + "a" * 500) * n
        # Warm-up once to settle first-call effects; min-of-3 so timer
        # noise on a loaded runner can't flake a linear implementation
        # (final review #6).
        _strip_html_noise(payload)
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            _strip_html_noise(payload)
            elapsed = time.perf_counter() - t0
            best = elapsed if best is None else min(best, elapsed)
        return best

    t_small = timed(400)
    t_big = timed(800)
    assert t_big < t_small * 3.5, (
        f"super-linear blowup: {t_small:.3f}s -> {t_big:.3f}s (ratio {t_big / t_small:.1f}x for 2x input)"
    )
    # Absolute ceiling: the ~400 KB pass must complete well under one CI
    # tick on any reasonable machine; a quadratic reimplementation would
    # be orders of magnitude slower.
    assert t_big < 0.25, f"linear scan too slow: {t_big:.3f}s for ~400 KB"


def test_strip_unclosed_script_neutralizes_to_end_and_keeps_preceding_text():
    """An unclosed <script> swallows the remainder (browser-consistent)."""
    out = _strip("<p>visible</p><script>alert(1);<p>hidden tail</p>")
    assert "visible" in out
    assert "alert(1)" not in out
    assert "hidden tail" not in out


def test_strip_comment_and_style_parities():
    # Unclosed comment swallows the remainder
    out = _strip("<p>keep</p><!-- secret <p>gone</p>")
    assert "keep" in out and "secret" not in out and "gone" not in out
    # Unclosed style swallows the remainder
    out2 = _strip("<p>keep2</p><style>.a{}<p>gone2</p>")
    assert "keep2" in out2 and ".a{}" not in out2 and "gone2" not in out2
    # Adjacent blocks without whitespace survive intact
    out3 = _strip("<script>a</script><style>b</style><!--c--><p>tail</p>")
    assert "tail" in out3 and "a" not in out3.split("tail")[0].replace("<script>a</script>", "")


_GROUPINGS = (
    HTTP_BLOCKED_CLASSIFICATIONS,
    BROWSER_BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
)


def test_every_grouping_member_is_an_enum_member():
    """No grouping may name a string the taxonomy does not know about."""
    for grouping in _GROUPINGS:
        for label in grouping:
            assert label in CrawlClassification.__members__, (
                f"{label!r} is listed in a classification grouping but is not a "
                f"CrawlClassification member"
            )


def test_groupings_and_the_status_table_cover_the_documented_taxonomy():
    """Members the status table deliberately leaves to their documented default
    are exactly the older fetch-level failure codes; everything else the enum
    advertises must be claimed by a grouping or by the table."""
    documented_default = {
        "robots_disallowed",
        "navigation_timeout",
        "browser_crash",
        "network_error",
        "connection_error",
        "http_status_error",
        "value_error",
        "tls_error",
        "dns_error",
        "invalid_url",
        "unsupported_scheme",
        "private_network_target",
        "unknown_error",
    }
    covered = set(CLASSIFICATION_STATUS_BY_LABEL)
    for grouping in _GROUPINGS:
        covered |= set(grouping)
    accounted = covered | documented_default
    entire = {member.value for member in CrawlClassification}
    assert accounted == entire, (
        f"taxonomy/accounting drift: "
        f"unaccounted={sorted(entire - accounted)} "
        f"secretly-defaulted={sorted(accounted - entire - documented_default)}"
    )


def test_capabilities_advertises_exactly_the_taxonomy():
    """The /v1/capabilities wire list is generated from the single enum, not a
    second hand-copied list."""
    from app.discovery.capabilities import capabilities_payload
    from app.settings import CrawlerSettings

    advertised = capabilities_payload(CrawlerSettings.from_mapping())["supported_classifications"]
    assert advertised == [member.value for member in CrawlClassification]


def test_groupings_are_derived_from_the_enum_not_relisted():
    """Each grouping is built from enum member references, not a hand-copied set
    of string literals -- the AST has no string constant inside a grouping."""
    import ast
    import inspect

    from app import constants

    tree = ast.parse(inspect.getsource(constants))
    assignments = {}
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target in (
            "HTTP_BLOCKED_CLASSIFICATIONS",
            "BROWSER_BLOCKED_CLASSIFICATIONS",
            "FAILED_CLASSIFICATIONS",
        ):
            assignments[target] = value
    for name in (
        "HTTP_BLOCKED_CLASSIFICATIONS",
        "BROWSER_BLOCKED_CLASSIFICATIONS",
        "FAILED_CLASSIFICATIONS",
    ):
        value = assignments[name]
        literals = [
            node.value
            for node in ast.walk(value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not literals, f"{name} embeds string literals: {literals}"

        grouped = [
            node.attr
            for node in ast.walk(value)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "CrawlClassification"
        ]
        assert grouped, f"{name} groups no enum members"
        unknown = [attr for attr in grouped if attr not in CrawlClassification.__members__]
        assert not unknown, f"{name} references non-members: {unknown}"


class TestClassificationComments:
    def test_html_without_script_style_signal(self):
        # Regression guard for the HTML-comment classification bypass fix:
        # a block signature inside an HTML comment must NOT be detected
        # (comments are not user-visible and are attacker-controllable).
        classification, evidence = classify_page_with_evidence(
            "<html><!-- access denied --></html>",
            "clean visible text",
        )
        assert classification == "rendered"

    def test_html_visible_signal_detected(self):
        # Visible (non-comment) block signals in the HTML body must still be
        # detected via the html_without_script_style haystack.
        classification, evidence = classify_page_with_evidence(
            "<html><body>access denied</body></html>",
            None,
        )
        assert classification == "access_denied"
        assert evidence[0]["source"] == "html_without_script_style"


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
