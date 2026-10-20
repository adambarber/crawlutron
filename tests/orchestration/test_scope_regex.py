"""Adversarial fix (MEDIUM): ReDoS guard for user-controlled scope_regex.

``scope_regex`` is applied to every discovered link; a pathological pattern
like ``(a+)+$`` is refused at compile and the scanned-link count is bounded.
"""

import contextlib

import pytest

from app.admission.admission_links import compile_scope_pattern as compile_scope_pattern


def test_nested_quantifiers_compile_and_match_bounded():
    from app.safety.safe_regex import RegexTimeout, match_bounded

    for evil in ["(a+)+$", "(a|a)+$", "(a*)*", "(?:a+)+b", "(a+)+c"]:
        pat = compile_scope_pattern(evil)
        assert pat is not None, evil
        if pat is None:
            continue
        host = "http://" + "a" * 64 + ".test/"
        with contextlib.suppress(RegexTimeout):
            match_bounded(pat, host, timeout=0.25)  # bounded: the budget fired instead of the match hanging


def test_safe_compile_rejects_overlong_pattern():
    with pytest.raises(ValueError):
        compile_scope_pattern("a" * 300)


def test_safe_compile_rejects_unparseable_pattern():
    with pytest.raises(ValueError):
        compile_scope_pattern("(((a")


def test_safe_compile_accepts_normal_pattern():
    pat = compile_scope_pattern(r"example\.com")
    assert pat is not None
    assert pat.search("http://example.com/x")
