"""MEDIUM fix: _robots_pattern_to_regex must not recompile on every call.

_robots_allows runs per discovered link during a recursive crawl, and each call
recompiled every robot rule's pattern. For a large crawl this compiles tens of
thousands of identical regexes. The compiled pattern is now cached so each
unique pattern is compiled once.
"""

import re
from unittest.mock import patch

from app.crawl.engine import _robots_allows, _robots_pattern_to_regex


def test_robots_pattern_to_regex_is_cached():
    p1 = _robots_pattern_to_regex("/admin/*")
    p2 = _robots_pattern_to_regex("/admin/*")
    # Same compiled object: no redundant recompilation.
    assert p1 is p2


def test_robots_allows_does_not_recompile_per_call():
    rules = [(True, "/public/*"), (False, "/admin/*"), (False, "/*.pdf")]
    with patch.object(re, "compile", wraps=re.compile) as spy:
        for _ in range(50):
            _robots_allows("http://example.com/admin/secret", rules)
    unique_patterns = {p for _, p in rules}
    # Each unique pattern compiled at most once, not 50 times.
    assert spy.call_count <= len(unique_patterns), spy.call_count
