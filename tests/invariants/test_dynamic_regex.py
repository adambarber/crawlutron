"""A regex pattern built from anything other than a literal is the ReDoS shape:
the hazard is a runtime-built *pattern* argument, so the rule fires on a dynamic
first arg to ``re.<method>`` only; companions close the alias and pre-bound-``compile`` bypasses.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import SANCTIONED, app_trees, call_nodes, nodes_of, offender, offender_at
from tests.invariants._shared import assert_trees_clean

# Dynamic-pattern re.* calls: a non-literal first arg is a pattern built from attacker-influenced text (the
# ReDoS shape). `re.search(LITERAL, text)` is safe -- only a dynamic PATTERN is the hazard.
_RE_DYNAMIC_METHODS = frozenset({"compile", "search", "match", "fullmatch", "sub", "findall", "split"})


def _re_dynamic_pattern_offenders(methods: frozenset[str]):
    """Build the detector for ``re.<method>(<non-literal pattern>, ...)``: the hazard
    is a pattern built from attacker-influenced text, and both the broad sweep and
    the single-method gate are that one predicate over the method set, written once.
    """

    def detector(tree: ast.AST, path: pathlib.Path) -> list[str]:
        return [
            offender_at(path, node, f"re.{node.func.attr}(<dynamic pattern>)")
            for node in call_nodes(tree)
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in methods
            and node.args
            and not isinstance(node.args[0], ast.Constant)
        ]

    return detector


_dynamic_pattern_re_calls = _re_dynamic_pattern_offenders(_RE_DYNAMIC_METHODS)


def test_no_dynamic_pattern_re_calls():
    """re.* with a non-literal pattern must go through app.safety.safe_regex."""
    assert_trees_clean(
        _dynamic_pattern_re_calls,
        "Use app.safety.safe_regex.compile_untrusted/match_bounded",
        trees=app_trees(),
    )


def test_no_direct_regex_engine_import():
    """Only safe_regex.py may import the third-party `regex` engine."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):  # only safe_regex legitimately imports `regex`
        for node in nodes_of(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "regex":
                        offenders.append(offender(path, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "regex":
                    offenders.append(offender(path, node.lineno))
    assert not offenders, "Only app.safety.safe_regex may import the regex engine: " + ", ".join(offenders)


def test_no_bare_re_compile_on_nonliteral():
    """Non-literal re.compile must go through app.safety.safe_regex.compile_untrusted."""
    assert_trees_clean(
        _re_dynamic_pattern_offenders(frozenset({"compile"})),
        "Use app.safety.safe_regex.compile_untrusted for non-literal patterns",
        trees=app_trees(),
    )
