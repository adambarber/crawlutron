"""The mypy scope covers every test module it claims (#314: widened to ``tests``)."""

from __future__ import annotations

import configparser

from tests.chokepoint_walks import REPO_ROOT

_MYPY_INI = REPO_ROOT / "mypy.ini"
_SCOPE_TERM = "tests"
_TESTS = REPO_ROOT / "tests"


def _declared_scope_files() -> str:
    """The value of mypy.ini's ``files`` key, read as INI so the key is named."""
    parser = configparser.ConfigParser()
    parser.read(_MYPY_INI)
    return parser["mypy"]["files"]


def _declared_scope_terms() -> tuple[str, ...]:
    """The ``files`` value's terms, comma-split and stripped."""
    return tuple(term.strip() for term in _declared_scope_files().split(","))


def _tree_test_modules() -> tuple[str, ...]:
    """Every ``tests/**/*.py`` module in the tree, as repo-relative posix strings."""
    return tuple(sorted(str(path.relative_to(REPO_ROOT).as_posix()) for path in _TESTS.rglob("*.py")))


def _scope_matches(module: str) -> bool:
    """Whether the scope's terms reach ``module``: a term matches a module when the
    module lives under that term's directory (mypy's ``files`` is directory-rooted)."""
    return any(module == term or module.startswith(f"{term}/") for term in _declared_scope_terms())


def test_the_scope_names_the_tests_tree() -> None:
    """mypy.ini must scope the whole tests tree, not a private-module glob that
    leaves every public test module checked only by transitive-import luck."""
    assert _SCOPE_TERM in _declared_scope_terms(), (
        "mypy.ini files = "
        + _declared_scope_files()
        + " does not contain the "
        + _SCOPE_TERM
        + " term; test modules outside the named set are then checked by luck"
    )


def test_the_scope_reaches_every_test_module_the_tree_holds() -> None:
    """The scope is total over tests/, module by module: every test module in the
    tree must be matched by a declared term, so a newly added module is evidence."""
    uncovered = sorted(module for module in _tree_test_modules() if not _scope_matches(module))
    assert not uncovered, (
        "mypy.ini files = "
        + _declared_scope_files()
        + " does not reach the test modules "
        + str(uncovered)
        + "; a module outside every declared term is checked only by luck"
    )


def test_the_scope_covers_the_real_test_tree() -> None:
    """The term is not vestigial: the subtrees it claims must actually hold test
    modules, and the reach proof's module must exist."""
    subtrees = {module.rsplit("/", 1)[0] for module in _tree_test_modules()}
    assert "tests" in subtrees, "the flat tests/ subtree is gone from the scope's reach"
    assert "tests/invariants" in subtrees, (
        "the invariants subtree is gone from the scope's reach; re-ground this gate in the live tree"
    )
    assert (REPO_ROOT / "tests" / "_policy_factories.py").exists(), (
        "the reach proof's module is gone; re-ground this gate in the live tree"
    )


def test_the_old_harness_glob_scope_fails_the_gate() -> None:
    """The pre-#314 scope (production + the private-module glob) fails the invariant:
    it left every public test module outside the checker."""
    old_scope = "crawlutron, tests/**/_*.py"
    assert _SCOPE_TERM not in (term.strip() for term in old_scope.split(",")), (
        "the old scope must not reach the tests tree"
    )
