"""Every detector walks the same trees and the same nodes, once."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import TESTS_DIR, app_python_files, app_trees, nodes_of, trees_under
from tests.invariants.test_tree_imports import _app_reads


def test_every_detector_sees_the_same_app_trees():
    """Two walks are one snapshot: the SAME ast.Module objects, not two parses.

    Identity, not equality. A second parse of an unchanged file compares equal
    for every field a detector looks at, so a detector cannot tell a
    re-observation from a changed tree -- which is exactly the distinction the
    shared snapshot exists to draw.
    """
    first = app_trees()
    second = app_trees()
    assert first, "the app tree walk found no modules to compare"
    assert second is first, "each caller is being handed its own walk of app/"
    assert all(one is other for one, other in zip(first, second, strict=True)), (
        "the same module parsed twice is not a shared snapshot"
    )


def test_the_app_tree_is_read_once_however_many_detectors_walk_it(monkeypatch):
    """N detectors cost one read per module, not N.

    Bounded by the module count rather than equal to it, so the assertion is
    true whether this test took the snapshot or an earlier one in the session
    did. Today each walk re-reads the tree, so three walks cost three times the
    modules and this is where the 37.7s went.
    """
    reads = _app_reads(monkeypatch)
    for _ in range(3):
        # Consumed, not merely called: a walk nobody iterates reads nothing, and this test has to be able to
        # see a re-walk.
        tuple(app_trees())
        tuple(app_python_files())
    assert len(reads) <= len(app_python_files()), (
        f"the app tree was read {len(reads)} times for {len(app_python_files())} modules; "
        "the traversal has to be owned by the chokepoint, not repeated per detector"
    )


def test_a_later_walk_cannot_observe_a_tree_that_moved(monkeypatch):
    """The race half: once the snapshot exists, no walk touches the tree again.

    The filesystem is made to refuse rather than made to change, so this proves
    the property without a second offender. A walk that re-globbed raises here;
    a walk that returns its snapshot cannot, because a file that disappears
    after the snapshot is not something a snapshot can observe.
    """
    warm = app_trees()
    assert warm, "the app tree walk found no modules"

    def refuse(*_args, **_kwargs):
        raise AssertionError("a later walk touched the tree instead of returning its snapshot")

    monkeypatch.setattr(pathlib.Path, "rglob", refuse)
    monkeypatch.setattr(pathlib.Path, "read_text", refuse)
    assert app_trees() is warm


def test_the_path_list_and_the_parsed_trees_are_one_traversal(monkeypatch):
    """Asking for paths does not parse, and asking again costs nothing.

    The line-budget sweep wants the file list and the detectors want the trees;
    both are the same rglob, so the list half must not drag a parse along with
    it and neither may be recomputed per caller.
    """
    reads = _app_reads(monkeypatch)
    paths = app_python_files()
    assert len(reads) == 0, f"listing the tree parsed {len(reads)} modules it did not need parsed"
    trees = app_trees()
    assert [path for path, _tree in trees] == list(paths), "the trees are not the files that were listed"
    # Exactly one read per module when this test took the snapshot, and none at all when an earlier test in
    # the session already did: both are "once", and the assertion has to hold whichever order the session ran
    assert len(reads) in (0, len(trees)), (
        f"{len(reads)} reads for {len(trees)} modules; the snapshot is read once, not once per caller"
    )
    assert app_python_files() is paths, "the file list is re-globbed per caller"
    assert app_trees() is trees, "the tree snapshot is rebuilt per caller"


def test_the_tests_tree_is_walked_once_too():
    """The tests/ sweep is the same deal: five detectors, one walk.

    Same return type as ``app_trees`` for the same reason, and the five
    callers of ``trees_under(TESTS_DIR)`` now share one snapshot instead of
    each re-parsing the tree they are all reading.
    """
    first = trees_under(TESTS_DIR)
    assert first, "the tests/ walk found no modules"
    assert trees_under(TESTS_DIR) is first, "the tests/ tree is re-walked per detector"
    assert trees_under(TESTS_DIR / "crawl") is not first, "a different tree must be its own snapshot"


def test_every_detector_sees_the_same_nodes():
    """One materialization per root, and it is the one ``ast.walk`` yields.

    Three claims, each of which fails differently. IDENTITY, so a second walk
    costs nothing and the sequence cannot have been rebuilt underneath a
    detector mid-iteration. ELEMENT-WISE EQUALITY against a fresh
    ``ast.walk(tree)``, so what is shared is the same objects in the same
    breadth-first order rather than a subset, a re-ordering or a set. And
    COVERAGE, because a memo that answered an empty tuple for a root it had not
    walked would be fast, green and blind -- the failure this whole file exists
    to make impossible, reintroduced through the cache.
    """
    expected_modules = len(app_python_files())
    walked = 0
    for path, tree in app_trees():
        shared = nodes_of(tree)
        assert shared is nodes_of(tree), f"{path.name}: the node sequence is rebuilt per caller"
        assert list(shared) == list(ast.walk(tree)), (
            f"{path.name}: the shared sequence is not what ast.walk yields"
        )
        assert shared, f"{path.name}: a materialized walk came back empty"
        walked += 1
    assert walked == expected_modules, (
        f"the shared node sequence covers {walked} of {expected_modules} app modules"
    )


def test_a_narrowed_walk_is_shared_too():
    """The sub-walks share on the same terms, and they are the common case.

    Most detectors here narrow to one function and walk that rather than
    walking the module, and those roots are memoized exactly like a module is.
    Two definitions per module rather than all of them: the point is that a
    narrowed root behaves like any other, and re-walking every function in the
    tree inside the test that exists to avoid re-walking them would be the
    thing it is checking.
    """
    checked = 0
    for path, tree in app_trees():
        narrowed = [
            node for node in nodes_of(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ][:2]
        for node in narrowed:
            shared = nodes_of(node)
            assert shared is nodes_of(node), f"{path.name}: a narrowed walk is rebuilt per caller"
            assert list(shared) == list(ast.walk(node)), (
                f"{path.name}:{node.lineno}: the narrowed sequence is not what ast.walk yields"
            )
            checked += 1
    assert checked >= 100, (
        f"only {checked} narrowed walks were checked across the tree; the assertion "
        "has to span the snapshot to be evidence about the snapshot"
    )
