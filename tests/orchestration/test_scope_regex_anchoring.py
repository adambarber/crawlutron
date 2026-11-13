"""scope_regex anchors to the host and ignores the query unless the caller
anchors explicitly: a natural host pattern must not over-admit egress."""

from crawlutron.admission.admission_links import compile_scope_pattern
from crawlutron.admission.scope_match import scope_allows


def _allows(pattern: str, url: str) -> bool:
    compiled = compile_scope_pattern(pattern)
    assert compiled is not None
    return bool(scope_allows(compiled, url))


def test_unanchored_host_pattern_refuses_a_sibling_host():
    assert not _allows(r"example\.com", "https://evil-example.com/one")


def test_unanchored_host_pattern_refuses_a_longer_prefix_host():
    assert not _allows(r"example\.com", "https://notexample.com/")


def test_unanchored_host_pattern_refuses_a_query_string_host():
    assert not _allows(r"example\.com", "https://other.com/path?x=example.com")


def test_unanchored_host_pattern_still_admits_a_well_formed_url():
    assert _allows(r"example\.com", "https://example.com/one")


def test_explicit_anchor_keeps_raw_semantics():
    compiled = compile_scope_pattern(r"^https://example\.com")
    assert scope_allows(compiled, "https://example.com/one?keep=query")


def test_end_anchored_pattern_is_not_fooled_by_a_query_tail():
    compiled = compile_scope_pattern(r"example\.com$")
    assert not scope_allows(compiled, "https://times.example.com/news?x=example.com")


def test_char_class_negation_is_not_a_position_anchor():
    # A ``^`` inside a character class is negation ([^/] = any-except-slash),
    # not a position anchor; the pattern must be wrapped to refuse the sibling.
    assert not _allows(r"example\.com[^/]*", "https://evil-example.com/one")
    assert _allows(r"example\.com[^/]*", "https://example.com/x")


def test_explicitly_anchored_pattern_is_subject_to_the_query_strip():
    # The query is always stripped, even for an anchored pattern, so a query-
    # scoped pattern matches only the query-stripped scheme://host/path target.
    compiled = compile_scope_pattern(r"example\.com/news\?x=1$")
    assert not scope_allows(compiled, "https://example.com/news?x=1")
