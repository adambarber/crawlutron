"""The mypy scope covers every test module it claims (#314: widened to ``tests``)."""

from __future__ import annotations

import configparser
import json

from tests._walk_primitives import import_roots
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


def _production_scope_terms() -> set[str]:
    """The ``files`` terms that are not the ``tests`` term, as a set."""
    return set(_declared_scope_terms()) - {_SCOPE_TERM}


def test_production_scope_terms_equal_the_derived_import_roots() -> None:
    """mypy.ini production terms EQUAL the derived import roots (#906, owner
    2026-11-12); a moved or new member reds here by name, never needs a hand-edit."""
    assert _production_scope_terms() == set(import_roots()), (
        "mypy.ini files production terms must equal the derived import roots; got "
        + repr(sorted(_production_scope_terms()))
        + " vs derived "
        + repr(sorted(import_roots()))
    )


def test_every_derived_import_root_is_a_scope_term() -> None:
    """The derive-forbid-everything direction, named so a planted mutation bites: a
    derived import root must never be silently dropped from mypy's scope."""
    terms = set(_declared_scope_terms())
    missing = sorted(set(import_roots()) - terms)
    assert not missing, f"derived import roots missing from mypy.ini files: {missing}"


def test_no_scope_term_is_foreign_to_the_derivation() -> None:
    """The no-stale-literal direction: a ``files`` term that names no derived import
    root is a hand-typed orphan (``crawlutron_sdk`` after the #898 move)."""
    foreign = sorted(_production_scope_terms() - set(import_roots()))
    assert not foreign, f"mypy.ini files terms that are not derived import roots: {foreign}"


_PYRIGHT_CONFIG = REPO_ROOT / "pyrightconfig.json"


def _jsonc_load_source(source: str) -> dict[str, object]:
    """Parse a JSONC document (stripping ``//`` line comments outside strings)."""
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if ch == '"' and not in_string:
            in_string = True
            out.append(ch)
        elif ch == '"' and in_string:
            in_string = False
            out.append(ch)
        elif in_string and ch == "\\":
            out.append(ch)
            if nxt:
                out.append(nxt)
                i += 1
        elif not in_string and ch == "/" and nxt == "/":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return json.loads("".join(out))


def _pyright_include_terms() -> set[str]:
    """The ``include`` terms of pyrightconfig.json (JSONC), as a set."""
    include = _jsonc_load_source(_PYRIGHT_CONFIG.read_text(encoding="utf-8"))["include"]
    assert isinstance(include, list) and all(isinstance(t, str) for t in include), (
        f"pyrightconfig.json include is not a string list: {include!r}"
    )
    return set(include)


def test_pyright_include_terms_are_a_subset_of_the_derived_import_roots() -> None:
    """pyrightconfig include is a NAMED SUBSET of import_roots() (owner 2026-11-12,
    option b): basedpyright enforces core while rest/sdk/admission are mypy's domain."""
    included = _pyright_include_terms()
    assert included, "pyrightconfig.json include is empty; basedpyright would analyze nothing"
    assert included <= set(import_roots()), "pyrightconfig.json include names non-derived roots: " + repr(
        sorted(included - set(import_roots()))
    )
