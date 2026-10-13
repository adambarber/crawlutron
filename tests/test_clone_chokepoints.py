import ast

import pytest

from tests._clone_funnel_detectors import DETECTORS, FUNCTION_TYPES
from tests._clone_funnel_table import FUNNEL_HOME_MODULES, FUNNELS
from tests.chokepoint_walks import TESTS_DIR, trees_under

NESTABLE_TYPES = (*FUNCTION_TYPES, ast.ClassDef)


def _scopes(nodes):
    """Every function in ``nodes``, at any nesting depth.

    Recursion continues *through* a function body, not just around it. A
    hand-rolled setup is nearly always a closure inside a test rather than a
    module-level helper, so a walk that stops at the first ``def`` reports a
    clean tree while the thing it was written to catch has moved one indent in.
    Every detector below shares this walk, so the gap was not one detector's
    blind spot -- it was the whole declaration file agreeing on an empty tree.
    """
    for node in nodes:
        if isinstance(node, NESTABLE_TYPES):
            yield node
        yield from _scopes(ast.iter_child_nodes(node))


def _scopes_outside_owners(nodes, owners, inside_owner=False):
    """Yield ``(node, exempt)`` for every function, tracking owner ancestry.

    A funnel is exempt by *name*, and a funnel is usually a factory whose own
    implementation is the closure it returns -- ``blocking_pinned_request``
    builds ``build``, and ``build`` is the thing the detector recognises. So
    exemption has to be lexical, not just self-referential: a function nested
    anywhere inside a named owner is that owner's own body, and flagging it
    would be the detector reporting the declaration file's own plumbing.
    """
    for node in nodes:
        child_inside = inside_owner
        if isinstance(node, NESTABLE_TYPES):
            yield node, inside_owner
            child_inside = inside_owner or node.name in owners
        yield from _scopes_outside_owners(list(ast.iter_child_nodes(node)), owners, child_inside)


def _detector_bites(name, tree):
    return any(DETECTORS[name](node) for node in _scopes(tree.body))


def _clone_funnel_offenders():
    owners = {owner for _name, owner, _source in FUNNELS}
    offenders = []
    for path, tree in trees_under(TESTS_DIR):
        if path.name in FUNNEL_HOME_MODULES:
            continue
        scopes = [(node, exempt) for node, exempt in _scopes_outside_owners(tree.body, owners) if not exempt]
        for index, (node, _) in enumerate(scopes):
            for name, owner, _source in FUNNELS:
                if node.name == owner or not DETECTORS[name](node):
                    continue
                # Only the innermost scope that matches is reported.
                if any(DETECTORS[name](inner) for inner, _ in scopes[index + 1 :]):
                    continue
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{node.lineno}:{name}")
    return offenders


@pytest.mark.parametrize(("name", "owner", "source"), FUNNELS)
def test_clone_funnel_detector_bites(name, owner, source):
    assert _detector_bites(name, ast.parse(source))


def test_extracted_test_funnels_stay_extracted():
    offenders = _clone_funnel_offenders()
    assert not offenders, "test clone funnel inlined: " + "; ".join(offenders)
