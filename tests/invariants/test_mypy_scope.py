"""The mypy scope covers every shared-harness module it claims (#397)."""

from __future__ import annotations

import configparser
import glob

from tests.chokepoint_walks import REPO_ROOT

_MYPY_INI = REPO_ROOT / "mypy.ini"
_SCOPE_GLOB = "tests/**/_*.py"
_TESTS = REPO_ROOT / "tests"


def _declared_scope_files() -> str:
    """The value of mypy.ini's ``files`` key, read as INI so the key is named."""
    parser = configparser.ConfigParser()
    parser.read(_MYPY_INI)
    return parser["mypy"]["files"]


def _declared_scope_globs() -> tuple[str, ...]:
    """The ``files`` value's glob terms, comma-split and stripped."""
    return tuple(term.strip() for term in _declared_scope_files().split(","))


def _tree_private_modules() -> tuple[str, ...]:
    """Every ``tests/**/_*.py`` module in the tree, as repo-relative posix strings."""
    return tuple(sorted(str(path.relative_to(REPO_ROOT).as_posix()) for path in _TESTS.rglob("_*.py")))


def _scope_matches(tree_private_module: str) -> bool:
    """Whether the scope's glob terms reach ``tree_private_module``, as mypy expands them."""
    return any(
        tree_private_module in glob.glob(term, root_dir=REPO_ROOT, recursive=True)
        for term in _declared_scope_globs()
    )


def test_the_mypy_scope_names_the_harness_glob() -> None:
    """mypy.ini must scope the harness recursively (``tests/**/_*.py``), not two
    named modules and not the flat term that never left tests/ top level."""
    assert _SCOPE_GLOB in _declared_scope_files(), (
        "mypy.ini files = "
        + _declared_scope_files()
        + " does not contain the "
        + _SCOPE_GLOB
        + " glob; a harness module is then checked only by transitive-import luck"
    )


def test_the_scope_reaches_every_private_module_the_tree_holds() -> None:
    """The scope is total over the harness, subtree by subtree: every
    ``tests/**/_*.py`` module in the tree must be matched by a declared glob term,
    with the covered set enumerated so a newly matched module is evidence, not luck."""
    tree_private_modules = _tree_private_modules()
    covered = {module for module in tree_private_modules if _scope_matches(module)}
    uncovered = sorted(set(tree_private_modules) - covered)
    assert not uncovered, (
        "mypy.ini files = "
        + _declared_scope_files()
        + " does not reach the harness modules "
        + str(uncovered)
        + "; a private module outside app/ and the flat tests/ top level is then"
        " checked only by transitive-import luck"
    )


def test_the_scope_covers_the_real_harness_tree() -> None:
    """The glob is not vestigial: each subtree it claims must actually hold harness
    modules, enumerated per subtree rather than one hardcoded file."""
    tree_private_modules = _tree_private_modules()
    covered = {module for module in tree_private_modules if _scope_matches(module)}
    subtrees = {module.rsplit("/", 1)[0] for module in covered}
    assert "tests" in subtrees, "the flat tests/ harness subtree is gone from the scope's reach"
    assert "tests/invariants" in subtrees, (
        "the invariants harness subtree is gone from the scope's reach; re-ground this gate in the live tree"
    )
    assert (REPO_ROOT / "tests" / "_policy_factories.py").exists(), (
        "the reach proof's module is gone; re-ground this gate in the live tree"
    )


def test_the_old_two_module_scope_fails_the_gate() -> None:
    """The pre-#397 scope (named modules, no glob) fails the invariant."""
    old_scope = "app, tests/_fetch_outcome.py, tests/_transport_doubles.py"
    assert _SCOPE_GLOB not in old_scope, "the old scope must not contain the glob"
