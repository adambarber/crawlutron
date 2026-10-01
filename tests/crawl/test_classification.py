"""Page classification: phrases, structural markers, taxonomy, status resolution and memory bounds."""

import ast
import inspect

import pytest

from app import constants as app_constants
from app.classify import (
    CLASSIFICATION_SIGNATURES,
    STRONG_CLASSIFICATION_MARKERS,
    classify_page,
    classify_page_with_evidence,
)
from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    CLASSIFICATION_STATUS_BY_LABEL,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    NON_TRANSIENT_CLASSIFICATIONS,
    CrawlClassification,
    status_for_classification,
)
from app.crawl.result_helpers import TerminalResult
from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies import http as http_mod
from app.crawl.strategies.browser import BrowserRenderer
from tests._result_builders import skipped_result
from tests._timing_harnesses import cpu_seconds
from tests.chokepoint_walks import app_trees, nodes_of


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


# The classification groupings are set OPERATIONS over one another, not one
# transcription of the taxonomy per grouping. Two properties enforce that, and
# they need two different detectors because they fail in different ways.
#
# 1. A grouping may not embed string literals, and must reach the taxonomy
#    through enum member references -- directly or by composing another
#    grouping. A hand-copied ``{"rate_limited", "access_denied"}`` is correct
#    until the taxonomy grows, and then it is silently short.
#
# 2. A member may not be SPELLED in two groupings. This is the half that a value
#    assertion cannot see and the half that actually bit: the relation tests
#    cannot tell a derived set from a transcribed one, because a set that
#    happens to be correct today is identical under ``in``, ``<=`` and ``==``.
#    What was wrong with the four hand-written lists is that only some of them
#    were operations. ``HTTP_BLOCKED`` was a union, so a new blocked label
#    reached it for free; ``NON_TRANSIENT`` re-listed the same eleven labels, so
#    the same new label had to be added there by hand and nothing failed when it
#    was not. A frozenset missing a member is still a valid frozenset.
#
# The walk is tree-wide over app/ rather than scoped to ``app/constants.py``: a
# grouping declared in a second module is the same drift at a different address,
# and a check that reads one file cannot see it.
_GROUPING_ASSIGNMENTS = frozenset(
    {
        "BROWSER_BLOCKED_CLASSIFICATIONS",
        "HTTP_BLOCKED_CLASSIFICATIONS",
        "FAILED_CLASSIFICATIONS",
        "TRANSIENT_CLASSIFICATIONS",
        "NON_TRANSIENT_CLASSIFICATIONS",
    }
)


def _grouping_declarations() -> dict[str, ast.AST]:
    """Every classification-grouping assignment in app/, as ``name -> value``."""
    out: dict[str, ast.AST] = {}
    for _path, tree in app_trees():
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in _GROUPING_ASSIGNMENTS:
                    out[target.id] = value
    return out


def _assigned_value(source: str, index: int = 0) -> ast.AST:
    """The value ``source``'s ``index``-th top-level statement assigns.

    The snippets below are single-assignment modules, and reaching their value
    was spelled out as ``ast.parse(...).body[i].value`` at each site. That
    expression is only a value on an ``Assign``, and saying so once here is what
    lets the tests be about the groupings rather than about the walk.
    """
    statement = ast.parse(source).body[index]
    assert isinstance(statement, ast.Assign), source
    return statement.value


def _spelled_classification_members(value: ast.AST) -> set[str]:
    """Every ``CrawlClassification.<member>`` named inside ``value``."""
    return {
        node.attr
        for node in nodes_of(value)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "CrawlClassification"
    }


def _string_literals(value: ast.AST) -> list[str]:
    return [
        node.value
        for node in nodes_of(value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _reaches_the_taxonomy(
    name: str, declarations: dict[str, ast.AST], seen: frozenset[str] = frozenset()
) -> bool:
    """Whether a grouping names enum members directly or via another grouping.

    The transitive half matters because a derived grouping legitimately contains
    no member reference of its own: ``HTTP_BLOCKED`` is a union of two earlier
    groupings, and requiring it to spell a member would force the transcription
    this whole section exists to prevent. A cycle terminates instead of hanging,
    and reports False -- a grouping that only ever refers to itself has not
    reached the taxonomy by any path.
    """
    if name in seen or name not in declarations:
        return False
    value = declarations[name]
    if _spelled_classification_members(value):
        return True
    return any(
        _reaches_the_taxonomy(referenced, declarations, seen | {name})
        for referenced in {
            node.id
            for node in nodes_of(value)
            if isinstance(node, ast.Name) and node.id in _GROUPING_ASSIGNMENTS
        }
    )


def test_groupings_are_derived_from_the_enum_not_relisted():
    """No grouping embeds a string literal, and every one reaches the taxonomy.

    The literal half is the historical check. The reachability half replaces a
    proxy that was narrower than its own intent: it used to demand a direct
    ``CrawlClassification.x`` reference inside every grouping, which a correct
    transitive union does not have, so enforcing it would have required
    re-introducing the transcription.
    """
    declarations = _grouping_declarations()
    assert set(declarations) == _GROUPING_ASSIGNMENTS, (
        "expected every grouping to be declared, found "
        "{sorted(set(_GROUPING_ASSIGNMENTS) - set(declarations))}"
    )
    for name, value in sorted(declarations.items()):
        literals = _string_literals(value)
        assert not literals, f"{name} embeds string literals: {literals}"
        unknown = [
            attr
            for attr in sorted(_spelled_classification_members(value))
            if attr not in CrawlClassification.__members__
        ]
        assert not unknown, f"{name} references non-members: {unknown}"
        assert _reaches_the_taxonomy(name, declarations), (
            f"{name} reaches no CrawlClassification member, directly or through another grouping"
        )


def test_a_grouping_that_embeds_a_literal_fails_the_check():
    """The literal half of the guard above must fire on a hand-copied grouping."""
    copied = _assigned_value('GROUP = frozenset({"rate_limited", "access_denied"})\n')
    assert _string_literals(copied) == ["rate_limited", "access_denied"]


def test_a_grouping_with_no_path_to_the_taxonomy_fails_the_check():
    """The reachability half must fire on a grouping that composes nothing."""
    declarations = {"LOOP": _assigned_value("LOOP = LOOP | frozenset()\n")}
    assert not _reaches_the_taxonomy("LOOP", declarations)


def test_a_classification_grouping_is_derived_not_restated():
    """No taxonomy member is spelled in two grouping declarations.

    Every grouping after the leaves is composed with a set operation, so a
    classification added to the taxonomy lands in the derived groupings without
    anyone editing them, and this sweep has nothing to report.
    """
    spelled: dict[str, list[str]] = {}
    for name, value in sorted(_grouping_declarations().items()):
        for member in sorted(_spelled_classification_members(value)):
            spelled.setdefault(member, []).append(name)
    restated = [f"{member} spelled in {names}" for member, names in sorted(spelled.items()) if len(names) > 1]
    assert not restated, "classification groupings re-state a member: " + "; ".join(restated)


def test_the_restated_grouping_detector_bites():
    """A member transcribed into two groupings must fail the sweep above."""
    source = (
        "FIRST = frozenset(m.value for m in (CrawlClassification.rendered,))\n"
        "SECOND = frozenset(m.value for m in (CrawlClassification.rendered,))\n"
    )
    first = _spelled_classification_members(_assigned_value(source))
    second = _spelled_classification_members(_assigned_value(source, 1))
    assert first & second == {"rendered"}


def test_the_grouping_relations_still_hold():
    """The relations the derived declarations must preserve, stated as values.

    Deriving the groupings is only safe if the sets are still the same sets.
    These are the properties the hand-written transcriptions were carrying, and
    they are what a refactor of ``app/constants.py`` could silently break.
    """
    entire = {member.value for member in CrawlClassification}
    assert BROWSER_BLOCKED_CLASSIFICATIONS <= HTTP_BLOCKED_CLASSIFICATIONS
    assert BROWSER_BLOCKED_CLASSIFICATIONS <= NON_TRANSIENT_CLASSIFICATIONS
    assert HTTP_BLOCKED_CLASSIFICATIONS <= NON_TRANSIENT_CLASSIFICATIONS
    assert FAILED_CLASSIFICATIONS <= NON_TRANSIENT_CLASSIFICATIONS
    assert not HTTP_BLOCKED_CLASSIFICATIONS & FAILED_CLASSIFICATIONS
    assert entire >= HTTP_BLOCKED_CLASSIFICATIONS | FAILED_CLASSIFICATIONS


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
    comes. The replacement scanner must stay linear.

    Measured in CPU seconds, not wall clock, and sized so the signal is
    unambiguous. Both choices were arrived at by being wrong first.

    *Wall clock* reports how busy the box was, not how much work the scanner
    did: a descheduled process keeps the wall clock running while its own CPU
    clock stands still, which is how a scanner spending a flat ~2ns per byte
    measured as 37x super-linear for a 2x input.

    *Size* is the subtler one. At the 400-tag payload this test used to build,
    a full pass cost ~0.4ms, and at that scale the growth term is the same
    order as the fixed one -- a linear implementation measured 4.5x for a 2x
    input. Measuring over 1MB->4MB instead puts the linear term 100x above the
    noise, and widening the range from 2x to 4x makes the assertion *stronger*,
    not weaker: linear lands near 4x, quadratic near 16x, and the band between
    them is wider than the one it replaces.
    """
    from app.classify import _strip_html_noise

    def cpu_for(n):
        payload = ("<script>" + "a" * 500) * n
        return cpu_seconds(lambda: _strip_html_noise(payload))

    t_small = cpu_for(2_000)
    t_big = cpu_for(8_000)
    assert t_big < t_small * 7, (
        f"super-linear blowup: {t_small:.6f}s -> {t_big:.6f}s (ratio {t_big / t_small:.1f}x for 4x input)"
    )
    # Absolute ceiling, set from the measured ~2ns/byte: a linear pass over 4MB
    # costs ~8ms, while the quadratic this guards against costs minutes. 50ms
    # is 6x the headroom a slower instruction set needs and still well under
    # the regression it exists to catch.
    assert t_big < 0.05, f"linear scan too slow: {t_big:.6f}s of CPU for ~4MB"


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
                f"{label!r} is listed in a classification grouping but is not a CrawlClassification member"
            )


def test_the_status_table_covers_every_taxonomy_member():
    """Completeness: every ``CrawlClassification`` member is an explicit key in
    the status table, so no label can fall through to
    ``UNKNOWN_CLASSIFICATION_STATUS``.

    The module docstring claims this and nothing used to enforce it: 13 members
    (the transport/strategy exception codes, the robots and policy-refusal
    labels and the browser retry codes) took the "blocked" default while the
    terminal builders asserted "failed"/"skipped"/"disallowed"/"retry_exhausted"
    for those same labels -- so a consumer re-deriving status from
    classification saw a different value than the wire. The set difference is
    derived from the enum, not hand-copied, so a member added to the taxonomy
    without a status entry fails here by name.
    """
    entire = {member.value for member in CrawlClassification}
    covered = set(CLASSIFICATION_STATUS_BY_LABEL)
    assert covered <= entire, (
        f"status table names labels the taxonomy does not know: {sorted(covered - entire)}"
    )
    assert entire - covered == set(), (
        f"classifications with no explicit status entry: {sorted(entire - covered)}"
    )


def test_every_table_status_is_a_declared_result_status():
    """No table entry may name a status the ``ResultStatus`` literal does not
    declare -- the wire enum ``CrawlStatus`` mirrors that literal, so an
    invented status would be silently coerced to ``failed`` at shaping."""
    import typing

    from app.constants import ResultStatus

    declared = set(typing.get_args(ResultStatus))
    bad = {
        label: status for label, status in CLASSIFICATION_STATUS_BY_LABEL.items() if status not in declared
    }
    assert not bad, f"table entries with an undeclared status: {bad}"


class TestStatusAgreesWithClassification:
    """The (classification, status) agreement the taxonomy guards at the
    terminal builders. These are the labels that used to disagree: the builders
    asserted one status and ``status_for_classification`` re-derived the
    "blocked" default for the same label."""

    # The exception chokepoint's labels: it builds a terminal failure, and the
    # table used to hand back "blocked" for every one of them.
    @pytest.mark.parametrize(
        "classification",
        [
            "network_error",
            "connection_error",
            "tls_error",
            "dns_error",
            "http_status_error",
            "value_error",
            "browser_crash",
            "unknown_error",
        ],
    )
    def test_exception_chokepoint_status_matches_the_table(self, classification):
        from app.constants import status_for_classification
        from app.crawl.result_helpers import engine_result_from_exception

        result = engine_result_from_exception(
            error=ConnectionError("boom"),
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            strategy=None,
            known_classification=classification,
        )
        assert result.status == status_for_classification(classification) == "failed"

    def test_robots_disallowed_builder_status_matches_the_table(self):
        from app.constants import status_for_classification

        result = skipped_result(
            "http://example.com/",
            "robots_disallowed",
            strategy="HTTP_DECLARED_CRAWLER",
            error="robots_txt_disallowed",
        )
        assert result.status == status_for_classification("robots_disallowed") == "skipped"

    def test_browser_retry_exhausted_status_matches_the_table(self):
        """The browser retry loop's terminal verdict for a navigation timeout."""
        from app.constants import status_for_classification
        from app.crawl.result_helpers import terminal_crawl_result

        result = terminal_crawl_result(
            url="http://example.com/",
            status="retry_exhausted",
            classification="navigation_timeout",
            attempts=2,
            elapsed_ms=10,
            domain="example.com",
            error="navigation_timeout",
        )
        assert result.status == status_for_classification("navigation_timeout") == "retry_exhausted"

    def test_taxonomy_allows_the_documented_context_varying_status(self):
        """``unknown_error`` legitimately also pairs with ``retry_exhausted``:
        the browser retry loop's verdict describes the attempt, not the page."""
        from app.constants import status_agrees_with_classification, status_for_classification

        assert status_for_classification("unknown_error") == "failed"
        assert status_agrees_with_classification("unknown_error", "failed")
        assert status_agrees_with_classification("unknown_error", "retry_exhausted")

    def test_taxonomy_rejects_a_disagreeing_pair(self):
        """A pair neither the table default nor the documented context set allows
        must not be constructible at the terminal builder."""
        from app.crawl.result_helpers import _failed_engine_result

        with pytest.raises(ValueError, match="disagrees with the taxonomy status"):
            _failed_engine_result(
                TerminalResult(
                    original_url="http://example.com/",
                    normalized_url="http://example.com/",
                    strategy=None,
                    # robots_disallowed's taxonomy status is "skipped", so a builder
                    # asserting "failed" for it is exactly the disagreement the guard
                    # exists to reject.
                    classification="robots_disallowed",
                    # A real taxonomy member (the construction gate rejects a
                    # non-member before the agreement guard is reached); the
                    # disagreement under test is status-vs-classification.
                    error="robots_disallowed",
                )
            )


def test_capabilities_advertises_exactly_the_taxonomy():
    """The /v1/capabilities wire list is generated from the single enum, not a
    second hand-copied list."""
    from app.discovery.capabilities import capabilities_payload
    from app.settings import CrawlerSettings

    advertised = capabilities_payload(CrawlerSettings.from_mapping())["supported_classifications"]
    assert advertised == [member.value for member in CrawlClassification]


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


def test_wire_classification_literal_matches_the_enum():
    """The ``Classification`` Literal in app.models is a hand-maintained second
    copy of the enum.

    url_budget_exceeded shipped as an enum member missing from the Literal, and
    the value-validity detector could not catch it -- it checks call-site
    literals against the enum, not the Literal against the enum. A member added
    to the enum but forgotten in the Literal would still pass CI, which is the
    exact duplication that produced the original defect.
    """
    import typing

    from app import models
    from app.constants import CrawlClassification

    args = typing.get_args(models.Classification)
    enum_values = {m.value for m in CrawlClassification}
    literal_values = set(args)

    assert literal_values == enum_values, (
        f"Classification Literal drifted from CrawlClassification: "
        f"missing from Literal={sorted(enum_values - literal_values)} "
        f"stale in Literal={sorted(literal_values - enum_values)}"
    )
