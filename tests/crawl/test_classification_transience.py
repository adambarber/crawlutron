"""Transience: is a page that failed this way worth another pass?

The verdict used to live in three hand-maintained lists that had already drifted
apart -- the browser strategy's ``TRANSIENT_CLASSIFICATIONS`` authorized retries
for a code nothing produced (``dns_error``) while omitting the code that
actually caught every transport failure (``connection_error``);
``result_helpers`` hardcoded ``retryable = classification ==
"browser_pool_exhausted"`` in both the crawl and render paths; and
``recursive._commit_recursive_result`` requeued only that one label too,
recording a capacity skip as a terminal result. The durable job runner imported
the browser strategy's set, so a one-second DNS blip marked a URL visited and
dropped it from a whole-site crawl.

These tests pin the single source of truth, its coverage over the taxonomy, and
the classifier that feeds it. The tree-wide ban on new hand-lists is
``tests/invariants/test_taxonomy.py::test_transience_verdict_has_one_home``.
"""

import socket

import httpx
import pytest

from app.constants import (
    CLASSIFICATION_STATUS_BY_LABEL,
    NON_TRANSIENT_CLASSIFICATIONS,
    TRANSIENT_CLASSIFICATIONS,
    CrawlClassification,
    is_transient_classification,
)
from app.crawl.result_helpers import _classify_error_code


class TestTransienceIsATaxonomyOwnedVerdict:
    """The verdict belongs beside ``CLASSIFICATION_STATUS_BY_LABEL`` so one
    derived sweep covers both axes and a new member cannot arrive with only one
    of them accounted for."""

    def test_every_taxonomy_member_has_a_transience_verdict(self):
        """Both sides are stated, so the pair is a complete verdict over the
        enum. A member landing in neither set would default to terminal, which
        is the fail-closed direction but still an untriaged decision nobody
        made -- so it fails here instead, exactly as a missing status entry
        does."""
        entire = {member.value for member in CrawlClassification}
        decided = TRANSIENT_CLASSIFICATIONS | NON_TRANSIENT_CLASSIFICATIONS
        assert entire - decided == set(), (
            f"classifications with no transient verdict: {sorted(entire - decided)}"
        )

    def test_the_two_verdict_sets_never_overlap(self):
        overlap = TRANSIENT_CLASSIFICATIONS & NON_TRANSIENT_CLASSIFICATIONS
        assert not overlap, f"classifications claimed both ways: {sorted(overlap)}"

    def test_transient_verdicts_only_name_taxonomy_members(self):
        entire = {member.value for member in CrawlClassification}
        assert TRANSIENT_CLASSIFICATIONS <= entire, (
            f"transient set names labels the taxonomy does not know: "
            f"{sorted(TRANSIENT_CLASSIFICATIONS - entire)}"
        )
        assert NON_TRANSIENT_CLASSIFICATIONS <= entire, (
            f"non-transient set names labels the taxonomy does not know: "
            f"{sorted(NON_TRANSIENT_CLASSIFICATIONS - entire)}"
        )

    def test_every_transient_failure_is_never_reported_as_a_dead_page(self):
        """A page eligible for another pass must not wear a status that reads as
        done: ``blocked``/``disallowed``/``skipped`` are the "we are finished
        with this" shapes a consumer keys on, and ``success`` is the only status
        that feeds strategy pinning and the content cache."""
        dead_page = {"success", "blocked", "disallowed", "skipped"}
        for label in sorted(TRANSIENT_CLASSIFICATIONS):
            assert CLASSIFICATION_STATUS_BY_LABEL[label] not in dead_page, (
                f"{label} is transient but reports status "
                f"{CLASSIFICATION_STATUS_BY_LABEL[label]!r}, which a consumer "
                "reads as a finished page"
            )

    def test_untriaged_label_is_not_retried(self):
        """Fails closed: a label in neither set is a new taxonomy member nobody
        has triaged, and retrying an untriaged failure is the direction that
        costs politeness."""
        assert is_transient_classification("a_label_nobody_has_triaged") is False


class TestClassifiedFailuresGetTheVerdictThatMatchesThem:
    """The regression that mattered: a transport failure was classified
    ``connection_error`` and then treated as terminal, so a whole-site job
    marked the URL visited and moved on after a single DNS or connect blip."""

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (httpx.ConnectTimeout("x"), "connection_error"),
            (httpx.ReadTimeout("x"), "connection_error"),
            (httpx.ConnectError("x"), "connection_error"),
            (ConnectionRefusedError(), "connection_error"),
            (ConnectionResetError(), "connection_error"),
            (TimeoutError(), "connection_error"),
            (httpx.RemoteProtocolError("x"), "connection_error"),
            (httpx.HTTPError("x"), "http_error"),
            (ValueError("x"), "value_error"),
        ],
    )
    def test_classified_code_is_transient_except_value_error(self, exc, expected):
        code = _classify_error_code(exc)
        assert code == expected
        assert is_transient_classification(code) is (expected != "value_error")

    def test_dns_failures_report_dns_error_not_connection_error(self):
        """``dns_error`` was a declared, wire-legal, unreachable member: nothing
        in app/ ever produced it, because name-resolution failures shared a
        branch with connect/timeout failures. That is a dead taxonomy entry
        *and* a diagnostic that says "connection" when the name never resolved,
        so the two are split."""
        assert _classify_error_code(socket.gaierror(-2, "Name or service not known")) == "dns_error"
        assert _classify_error_code(socket.herror("no address associated with hostname")) == "dns_error"

    def test_dns_error_has_a_producer_in_app(self):
        """The guard for the defect itself: a declared member with no producer
        anywhere in app/ is a member of the wrong taxonomy. If the split above
        is ever reverted, this fails."""
        import ast

        from tests.chokepoint_walks import REPO_ROOT, app_python_files

        hits: list[str] = []
        for path in app_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "dns_error":
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert hits, "dns_error is a declared taxonomy member with no producer in app/"


class TestPageVerdictsStayTerminal:
    """The other half of the table: a page that was *characterized* is not a
    retry candidate, and requeueing one of these would either violate robots or
    hammer an origin that just refused us."""

    @pytest.mark.parametrize(
        "label",
        [
            "not_found",
            "gone",
            "access_denied",
            "rate_limited",
            "captcha_detected",
            "waf_challenge_detected",
            "robots_disallowed",
            "invalid_url",
            "unsupported_scheme",
            "private_network_target",
            "payload_too_large",
            "value_error",
            "site_error",
        ],
    )
    def test_characterized_pages_are_not_transient(self, label):
        assert is_transient_classification(label) is False

    def test_robots_disallowed_is_never_transient(self):
        """Requeueing a robots-disallowed URL would re-attempt exactly what the
        policy forbids. Called out separately because it is the one label where
        a "just retry it" reflex is an actual violation."""
        assert "robots_disallowed" not in TRANSIENT_CLASSIFICATIONS


class TestCapacitySkipsAreTransient:
    """``browser_pool_exhausted`` and ``circuit_breaker_open`` mean the URL was
    never attempted. Recording either as terminal marked the URL visited and
    silently dropped it from a whole-site crawl."""

    @pytest.mark.parametrize("label", ["browser_pool_exhausted", "circuit_breaker_open"])
    def test_capacity_decisions_are_transient(self, label):
        assert is_transient_classification(label) is True
        assert CLASSIFICATION_STATUS_BY_LABEL[label] == "failed"
