import ast

import pytest

from crawlutron.extraction.classify import CLASSIFICATION_SIGNATURES, STRONG_CLASSIFICATION_MARKERS
from crawlutron.transport import browser as browser_mod
from crawlutron.transport import http as http_mod
from crawlutron.transport.results import TerminalResult
from crawlutron.vocabulary.taxonomy import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    CLASSIFICATION_STATUS_BY_LABEL,
    FAILED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    NON_TRANSIENT_CLASSIFICATIONS,
    UNKNOWN_CLASSIFICATION_STATUS,
    CrawlClassification,
    status_for_classification,
)
from tests._classification_sets import GROUPING_ASSIGNMENTS as _GROUPING_ASSIGNMENTS
from tests._classification_sets import GROUPINGS as _GROUPINGS
from tests._classification_sets import UNRECOGNIZED_LABELS
from tests._policy_factories import canonical
from tests._result_builders import skipped_result
from tests.chokepoint_walks import app_trees, nodes_of


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


def test_the_status_resolver_is_the_taxonomy_resolver():
    """Both strategy paths resolve status through the one shared function."""
    assert browser_mod.status_for_classification is status_for_classification
    assert http_mod.status_for_classification is status_for_classification


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

    The snippets are single-assignment modules; saying "value only on an
    ``Assign``" once lets the tests be about the groupings, not the walk."""
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

    A derived grouping contains no member reference of its own
    (``HTTP_BLOCKED`` is a union); a cycle terminates and reports False."""
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

    Reachability replaces a proxy that demanded a direct member reference
    inside every grouping -- which a transitive union does not have."""
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

    Every grouping after the leaves is a set operation, so a classification
    lands in the derived groupings without anyone editing them."""
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

    The properties the hand-written transcriptions carried; what a refactor of
    ``crawlutron/vocabulary`` could silently break."""
    entire = {member.value for member in CrawlClassification}
    assert BROWSER_BLOCKED_CLASSIFICATIONS <= HTTP_BLOCKED_CLASSIFICATIONS
    assert BROWSER_BLOCKED_CLASSIFICATIONS <= NON_TRANSIENT_CLASSIFICATIONS
    assert HTTP_BLOCKED_CLASSIFICATIONS <= NON_TRANSIENT_CLASSIFICATIONS
    assert FAILED_CLASSIFICATIONS <= NON_TRANSIENT_CLASSIFICATIONS
    assert not HTTP_BLOCKED_CLASSIFICATIONS & FAILED_CLASSIFICATIONS
    assert entire >= HTTP_BLOCKED_CLASSIFICATIONS | FAILED_CLASSIFICATIONS


def test_every_classifiable_label_is_an_explicit_table_entry():
    """Drift guard: no classifier output may fall through to the default.

    Adding a signature to ``crawlutron.extraction.classify`` without a status is the
    regression this pins -- it is what let the two if-chains drift apart."""
    table = CLASSIFICATION_STATUS_BY_LABEL
    missing = [label for label in _classifiable_labels() if label not in table]
    assert not missing, f"classifications with no explicit status entry: {missing}"


def test_every_classifiable_label_resolves_through_the_table(browser_resolver):
    """Every label the classifiers can emit takes its status from the one table.

    With one shared resolver the HTTP/browser agreement is by construction; the
    assertion is that the resolver both paths use is the table itself."""
    labels = _classifiable_labels() + sorted(FAILED_CLASSIFICATIONS)
    labels += sorted(HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS)
    labels += list(UNRECOGNIZED_LABELS)
    for label in labels:
        expected = CLASSIFICATION_STATUS_BY_LABEL.get(label, UNKNOWN_CLASSIFICATION_STATUS)
        assert status_for_classification(label) == expected
        assert browser_resolver(label) == expected


def test_unknown_classification_has_one_documented_default(browser_resolver):
    """A label nobody knows about must resolve to the documented default."""
    for label in UNRECOGNIZED_LABELS:
        status = status_for_classification(label)
        assert status == browser_resolver(label) == UNKNOWN_CLASSIFICATION_STATUS
        # The safe default may never be "success": an unrecognized label has not produced verified content.
        assert status != "success"


def test_known_group_statuses_are_unchanged(browser_resolver):
    """Inverse case: the working paths must keep their exact statuses."""
    assert status_for_classification("rendered") == "success"
    assert browser_resolver("rendered") == "success"
    # Conditional freshness is a success: the caller's validators proved it.
    assert status_for_classification("not_modified") == "success"
    assert status_for_classification("not_modified") == "success"
    for classification in BROWSER_BLOCKED_CLASSIFICATIONS:
        assert browser_resolver(classification) == "blocked"
    for classification in HTTP_BLOCKED_CLASSIFICATIONS:
        assert status_for_classification(classification) == "blocked"
    for classification in FAILED_CLASSIFICATIONS:
        assert status_for_classification(classification) == "failed"
        assert browser_resolver(classification) == "failed"


class TestStatusForClassification:
    def test_http_status_not_found(self):
        from crawlutron.vocabulary.taxonomy import status_for_classification

        status = status_for_classification("not_found")
        assert status == "failed"

    def test_http_status_site_error(self):
        from crawlutron.vocabulary.taxonomy import status_for_classification

        status = status_for_classification("site_error")
        assert status == "failed"


def test_every_grouping_member_is_an_enum_member():
    """No grouping may name a string the taxonomy does not know about."""
    for grouping in _GROUPINGS:
        for label in grouping:
            assert label in CrawlClassification.__members__, (
                f"{label!r} is listed in a classification grouping but is not a CrawlClassification member"
            )


def test_the_status_table_covers_every_taxonomy_member():
    """Completeness: every taxonomy member is an explicit status-table key.

    Nothing used to enforce it -- 13 members took a "blocked" default while
    builders asserted otherwise; the enum-derived set difference fails by name."""
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

    from crawlutron.vocabulary.taxonomy import ResultStatus

    declared = set(typing.get_args(ResultStatus))
    bad = {
        label: status for label, status in CLASSIFICATION_STATUS_BY_LABEL.items() if status not in declared
    }
    assert not bad, f"table entries with an undeclared status: {bad}"


class TestStatusAgreesWithClassification:
    """The (classification, status) agreement the taxonomy guards at the
    terminal builders -- the labels that used to disagree, where the builders
    asserted one status and ``status_for_classification`` re-derived another."""

    # The exception chokepoint's labels: it builds a terminal failure, and the table used to hand back
    # "blocked" for every one of them.
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
        from crawlutron.transport.results import engine_result_from_exception
        from crawlutron.vocabulary.taxonomy import status_for_classification

        result = engine_result_from_exception(
            error=ConnectionError("boom"),
            original_url="http://example.com/",
            normalized_url=canonical("http://example.com/"),
            strategy=None,
            known_classification=classification,
        )
        assert result.status == status_for_classification(classification) == "failed"

    def test_robots_disallowed_builder_status_matches_the_table(self):
        from crawlutron.vocabulary.taxonomy import status_for_classification

        result = skipped_result(
            "http://example.com/",
            "robots_disallowed",
            strategy="HTTP_DECLARED_CRAWLER",
            error="robots_txt_disallowed",
        )
        assert result.status == status_for_classification("robots_disallowed") == "skipped"

    def test_browser_retry_exhausted_status_matches_the_table(self):
        """The browser retry loop's terminal verdict for a navigation timeout."""
        from crawlutron.models.result_factory import terminal_crawl_result
        from crawlutron.vocabulary.taxonomy import status_for_classification

        result = terminal_crawl_result(
            url=canonical("http://example.com/"),
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
        from crawlutron.vocabulary.taxonomy import (
            status_agrees_with_classification,
            status_for_classification,
        )

        assert status_for_classification("unknown_error") == "failed"
        assert status_agrees_with_classification("unknown_error", "failed")
        assert status_agrees_with_classification("unknown_error", "retry_exhausted")

    def test_taxonomy_rejects_a_disagreeing_pair(self):
        """A pair neither the table default nor the documented context set allows
        must not be constructible at the terminal builder."""
        from crawlutron.transport.results import failed_engine_result

        with pytest.raises(ValueError, match="disagrees with the taxonomy status"):
            failed_engine_result(
                TerminalResult(
                    original_url="http://example.com/",
                    normalized_url=canonical("http://example.com/"),
                    strategy=None,
                    # robots_disallowed's taxonomy status is "skipped", so a builder asserting "failed" for it
                    # is exactly the disagreement the guard exists to reject.
                    classification="robots_disallowed",
                    error="robots_disallowed",
                )
            )


def test_capabilities_advertises_exactly_the_taxonomy():
    """The /v1/capabilities wire list is generated from the single enum, not a
    second hand-copied list."""
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.discovery.capabilities import capabilities_payload

    advertised = capabilities_payload(CrawlerSettings.from_mapping())["supported_classifications"]
    assert advertised == [member.value for member in CrawlClassification]
