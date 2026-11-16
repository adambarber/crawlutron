"""suite_trees() yields cached AST objects via parsed() (#1098)."""

from tests.chokepoint_walks import REPO_ROOT, trees_under
from tests.invariants._clock_family import suite_trees


def test_suite_trees_same_ast_identity_as_trees_under():
    """Every path in suite_trees() must yield the exact ast.Module
    object that trees_under(REPO_ROOT / "tests") produces for it."""
    suite_map = {path: tree for path, tree in suite_trees()}
    trees_map = {path: tree for path, tree in trees_under(REPO_ROOT / "tests")}
    for path in suite_map:
        assert path in trees_map, f"missing from trees_under: {path}"
        assert suite_map[path] is trees_map[path], (
            f"identity mismatch for {path}: "
            f"suite_trees()={id(suite_map[path])}, "
            f"trees_under()={id(trees_map[path])}"
        )
