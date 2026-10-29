"""Every detector walks the same trees and the same nodes, once."""

from __future__ import annotations

import ast
import pathlib

import tests._walk_trees as _walk_trees
from tests.chokepoint_walks import (
    APP_ROOT,
    TESTS_DIR,
    app_offenders,
    app_python_files,
    app_trees,
    nodes_of,
    trees_under,
)
from tests.membership_gate import aliased_sanctioned_func_offenders
from tests.trees import app_reads as _app_reads


def test_every_detector_sees_the_same_app_trees():
    """Two walks are one snapshot: the SAME ast.Module objects, not two parses.

    Identity, not equality -- a second parse compares equal on every field a
    detector reads, so equality cannot tell a re-observation from a change."""
    first = app_trees()
    second = app_trees()
    assert first, "the app tree walk found no modules to compare"
    assert second is first, "each caller is being handed its own walk of app/"
    assert all(one is other for one, other in zip(first, second, strict=True)), (
        "the same module parsed twice is not a shared snapshot"
    )


def test_the_app_tree_is_read_once_however_many_detectors_walk_it(monkeypatch):
    """N detectors cost one read per module, not N.

    Bounded by module count, so the assertion holds whoever took the snapshot;
    three walks used to cost three times the modules (the 37.7s)."""
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

    The filesystem is made to REFUSE rather than change, so a re-globbing walk
    raises here; a snapshot cannot observe a post-snapshot disappearance."""
    warm = app_trees()
    assert warm, "the app tree walk found no modules"

    def refuse(*_args, **_kwargs):
        raise AssertionError("a later walk touched the tree instead of returning its snapshot")

    monkeypatch.setattr(pathlib.Path, "rglob", refuse)
    monkeypatch.setattr(pathlib.Path, "read_text", refuse)
    assert app_trees() is warm


def test_the_path_list_and_the_parsed_trees_are_one_traversal(monkeypatch):
    """Asking for paths does not parse, and asking again costs nothing.

    The line-budget sweep wants the file list, the detectors the trees; both
    are the same rglob, so neither half drags the other along or recomputes."""
    reads = _app_reads(monkeypatch)
    paths = app_python_files()
    assert len(reads) == 0, f"listing the tree parsed {len(reads)} modules it did not need parsed"
    trees = app_trees()
    assert [path for path, _tree in trees] == list(paths), "the trees are not the files that were listed"
    # Count bands 0..N: the shared parse cache means an earlier test on this
    # worker may have pre-parsed some files — through N is "once", above is a re-parse.
    assert len(reads) <= len(trees), (
        f"{len(reads)} reads for {len(trees)} modules; a listing may parse at most once per module"
    )
    assert app_python_files() is paths, "the file list is re-globbed per caller"
    assert app_trees() is trees, "the tree snapshot is rebuilt per caller"


def test_the_tests_tree_is_walked_once_too():
    """The tests/ sweep is the same deal: five detectors, one walk.

    Same return type as ``app_trees`` for the same reason; its five callers
    now share one snapshot instead of each re-parsing the same tree."""
    first = trees_under(TESTS_DIR)
    assert first, "the tests/ walk found no modules"
    assert trees_under(TESTS_DIR) is first, "the tests/ tree is re-walked per detector"
    assert trees_under(TESTS_DIR / "invariants") is not first, "a different tree must be its own snapshot"


def test_every_detector_sees_the_same_nodes():
    """One materialization per root, and it is the one ``ast.walk`` yields.

    IDENTITY (nothing rebuilt mid-iteration), element-wise equality with a
    fresh walk, and COVERAGE (an empty memo would be green and blind)."""
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

    Detectors narrow to one function and walk that; those roots are memoized
    like any module, two per module rather than all of them."""
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


def test_a_non_regex_detector_sees_safe_regex_scope(monkeypatch):
    """The sanctioned exclude is the regex rules', not every detector's: a planted
    banned-primitive use in safe_regex scope must be caught through the default walk."""
    planted = ast.parse(
        "from crawlutron.transport.decompression import decompress_body as dec\n"
        "def f(raw, enc):\n    return dec(raw, enc)\n"
    )
    path = APP_ROOT / "safety" / "safe_regex.py"
    real_trees = _walk_trees._app_trees

    def trees_with_plant(exclude):
        # Mirror the walk contract: names in the exclude set never reach a detector.
        if path.name in exclude:
            return real_trees(exclude)
        return (*real_trees(exclude), (path, planted))

    monkeypatch.setattr(_walk_trees, "_app_trees", trees_with_plant)
    offenders = app_offenders(aliased_sanctioned_func_offenders)
    assert offenders, "a planted decompress alias in safe_regex scope went unseen"
    assert any("dec" in hit for hit in offenders), offenders
