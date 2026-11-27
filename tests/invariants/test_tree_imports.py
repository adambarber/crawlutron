"""Test imports use package paths; private names stay in their package."""

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_paths import REPO_ROOT
from tests.chokepoint_walks import from_imports, offender_at, plain_imports, trees_under
from tests.rules import Bites, Registry, Rule
from tests.trees import test_tree_module_names as _test_tree_module_names
from tests.trees import tree_under_tests as _tests_tree
from utils.paths import member_src_roots

# Measured on tests/invariants/ alone: 37.7s of a 49.4s file, led by
# test_property_registry_fingerprints_are_observed 8.06s, test_documented_browser_knobs_have_consumers


def _imported_modules(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Every module name a tree imports, with the node that imported it.

    One traversal over both spellings (``from a.b import c`` contributes
    ``a.b``) so the caller compares one thing per import form."""
    for from_node in from_imports(tree):
        if from_node.module:
            yield from_node, from_node.module
    # A distinct name from the loop above: the two are separate traversals of the same tree, and one binding
    # for both meant the second yield carried the first loop's node type.
    for plain_node in plain_imports(tree):
        for alias in plain_node.names:
            yield plain_node, alias.name


def _bare_sibling_import_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A test module importing a sibling helper by its bare name.

    It resolves only via pytest's per-file ``sys.path`` hack -- invisible to
    E402 and mypy, and it breaks when a file moves."""
    local = _test_tree_module_names()
    return [
        offender_at(
            path,
            node,
            f"imports the test helper '{module}' by bare name; use its package path",
        )
        for node, module in _imported_modules(tree)
        if module in local
    ]


def _review_support_imports(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Shipped code reaching the review support contract."""
    return [
        offender_at(path, node, "imports review support from shipped code")
        for node, module in _imported_modules(tree)
        if module == "tests._support._review_contract" or module.startswith("crawlutron.review")
    ]


def _shipped_trees(*, exclude: frozenset[str]) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """Every shipped source tree, derived from the workspace members."""
    return tuple((path, tree) for root in member_src_roots() for path, tree in trees_under(root))


REVIEW_SUPPORT_IS_TEST_ONLY = Rule(
    name="review-support-is-test-only",
    find=_review_support_imports,
    summary="review support is developer tooling; a shipped package must not import it",
    scope=_shipped_trees,
    bite=lambda: Bites(
        "from crawlutron.review.types import Verdict\n"
        "from tests._support._review_contract import Verdict\n"
        "import crawlutron.review.support\n",
        marker="imports review support from shipped code",
        count=3,
    ),
    clean=lambda: [
        "from __future__ import annotations\n",
        "from crawlutron.admission.policy import read_policy\n",
        "from utils.paths import member_src_roots\n",
    ],
    proof_path=REPO_ROOT / "tests" / "invariants" / "_synthetic.py",
)


TEST_HELPERS_IMPORTED_BY_PACKAGE_PATH = Rule(
    name="test-helpers-imported-by-package-path",
    find=_bare_sibling_import_offenders,
    summary=(
        "import the helper through its package path (tests.<pkg>.<module>): a bare sibling name "
        "resolves only because pytest put this file's directory on sys.path, which is invisible to "
        "both E402 and the type checker"
    ),
    scope=_tests_tree,
    bite=lambda: Bites(
        "from _chain_policies import chain_policy\n"
        "import _job_harnesses as harnesses\n"
        "from _policy_factories import _policy\n",
        marker="imports the test helper",
        count=3,
    ),
    clean=lambda: (
        # Package, stdlib and third-party forms name a module that is not a test file, so the rule cannot
        # degenerate into "no unqualified imports".
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "import httpx\n"
        "import pytest\n"
        "from tests._policy_factories import _policy\n"
        "from tests._chain_policies import chain_policy\n"
        "from crawlutron.admission.policy import read_policy\n",
    ),
)


RULES = Registry((TEST_HELPERS_IMPORTED_BY_PACKAGE_PATH, REVIEW_SUPPORT_IS_TEST_ONLY))


def test_no_rule_violations():
    """No module imports a test sibling by bare name.

    The load-bearing half of a package-path import -- the thing a reviewer
    cannot check at a glance when reading an import."""
    RULES.check_all_clean()
