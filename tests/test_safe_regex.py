"""Contract tests for the bounded-execution regex chokepoint (N-1)."""

import threading
import time

import pytest

from app.safe_regex import (
    RegexTimeout,
    UnsafePattern,
    compile_untrusted,
    match_bounded,
)


class TestCompileUntrusted:
    def test_none_passthrough(self):
        assert compile_untrusted(None) is None

    def test_valid_pattern_compiles(self):
        compiled = compile_untrusted(r"^https://example\.com/")
        assert compiled is not None, "a safe pattern must compile"
        assert compiled.search("https://example.com/x")

    def test_overlong_pattern_rejected(self):
        with pytest.raises(UnsafePattern):
            compile_untrusted("a" * 257)

    def test_empty_pattern_rejected(self):
        with pytest.raises(UnsafePattern):
            compile_untrusted("")

    def test_unparseable_pattern_rejected(self):
        with pytest.raises(UnsafePattern):
            compile_untrusted("(((a")


class TestMatchBounded:
    def test_benign_match_completes(self):
        compiled = compile_untrusted(r"https://(\w+)\.test/")
        assert match_bounded(compiled, "https://foo.test/x")

    def test_none_pattern_returns_none(self):
        assert match_bounded(None, "anything") is None

    def test_fullmatch_and_match_ops(self):
        compiled = compile_untrusted(r"\d+")
        assert match_bounded(compiled, "123", op="fullmatch")
        assert match_bounded(compiled, "a123b", op="search")

    def _assert_bounded_timeout(self, pattern: str, text: str) -> None:
        """A pattern that must hit the bound, and must hit it without a leftover thread.

        Both halves are the point. A thread-abandonment implementation would
        pass the wall-time half alone while leaving a wedged worker burning
        behind -- so pin the wall time AND that no thread survived, and
        assert them in one place so a new catastrophic shape cannot pin one and
        forget the other.
        """
        compiled = compile_untrusted(pattern)
        threads_before = threading.active_count()
        start = time.perf_counter()
        with pytest.raises(RegexTimeout):
            match_bounded(compiled, text, timeout=0.05)
        elapsed = time.perf_counter() - start
        # Tight bound: the native C-loop timeout fires at ~the budget.
        assert elapsed < 0.5, f"timeout did not bound the match: {elapsed:.2f}s"
        time.sleep(0.2)  # let any abandoned worker reveal itself
        assert threading.active_count() <= threads_before, "match left a thread behind"

    def test_pathological_pattern_times_out_instead_of_hanging(self):
        # The N-1 class: nested quantifiers meeting a non-matching tail.
        # Under stdlib re the depth-3 shape .*(((a|aa)))*b is catastrophic
        # (0.136s measured at just 24 chars, doubling per character); the
        # regex engine optimizes that exact shape, so this test pins the
        # bound with an optimizer-resistant variant of the same class.
        self._assert_bounded_timeout(r"^(a|aa)+((a|aa)+)*x", "a" * 60 + "!")

    def test_timeout_bounds_exponential_backtracking(self):
        # A shape the regex engine cannot optimize away: (a|aa)+b against a
        # non-matching run of a's backtracks exponentially. Bounded, it must
        # raise RegexTimeout at ~the budget instead of hanging.
        self._assert_bounded_timeout(r"(a|aa)+b", "a" * 80 + "c")

    def test_matching_input_unaffected_by_timeout(self):
        # Same pattern, matching input: completes immediately (the bound
        # must not degrade legitimate matches).
        compiled = compile_untrusted(r"(a|aa)+b")
        assert match_bounded(compiled, "a" * 80 + "b", timeout=0.05)
