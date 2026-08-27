"""TestClassifyGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestClassifyGaps:
    def test_html_without_script_style_signal(self):
        # Regression guard for the HTML-comment classification bypass fix:
        # a block signature inside an HTML comment must NOT be detected
        # (comments are not user-visible and are attacker-controllable).
        classification, evidence = classify_page_with_evidence(
            "<html><!-- access denied --></html>", "clean visible text",
        )
        assert classification == "rendered"

    def test_html_visible_signal_detected(self):
        # Visible (non-comment) block signals in the HTML body must still be
        # detected via the html_without_script_style haystack.
        classification, evidence = classify_page_with_evidence(
            "<html><body>access denied</body></html>", None,
        )
        assert classification == "access_denied"
        assert evidence[0]["source"] == "html_without_script_style"


