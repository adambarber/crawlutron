"""Vocabulary primitives are referenced; retired buckets are not imported."""

import ast
import pathlib
from collections.abc import Iterator
from functools import lru_cache

from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    from_imports,
    offender_at,
    plain_imports,
    resolved_expr_name,
    trees_under,
)
from tests.invariants.test_tree_immutability import _tests_tree
from tests.invariants.test_tree_imports import _test_tree_module_names
from tests.rules import Bites, Registry, Rule, Silent

# =========================================================================== NO PUBLIC PRIMITIVE IN THE
# VOCABULARY LAYER MAY GO UNREFERENCED The suite has a convention rather than a registry for this: a leading
_VOCABULARY_LAYER = frozenset(
    {
        "chokepoint_walks.py",
        "membership_gate.py",
        "property_registry.py",
        "rules.py",
    }
)


def _referenced_names(tree: ast.AST, path: pathlib.Path) -> frozenset[str]:
    """Every name this module could be reaching for, in all three spellings.

    Bare ``Name``, ``alias`` tail (the original of ``from x import y as z``),
    and qualified forms resolved through imports -- ``attr`` alone lied."""
    local = _test_tree_module_names()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            tail = node.name.rsplit(".", 1)[-1]
            # A local module bound by a bare import names a file, not a primitive.
            if tail not in local:
                names.add(tail)
        elif isinstance(node, ast.Attribute):
            dotted = resolved_expr_name(tree, node)
            if dotted.startswith("tests.") or dotted.split(".", 1)[0] in local:
                names.add(dotted.rsplit(".", 1)[-1])
    return frozenset(names)


@lru_cache(maxsize=1)
def _reference_index() -> dict[pathlib.Path, frozenset[str]]:
    """What every test module names, keyed by the module that names it.

    Cached for the reason ``_test_tree_module_names`` is: the question is asked
    once per public primitive and the answer cannot change within a run.
    """
    return {path: _referenced_names(tree, path) for path, tree in _tests_tree(exclude=frozenset())}


def _public_definitions(
    tree: ast.Module,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """The top-level definitions a module advertises by not hiding them.

    One implementation on purpose -- the detector and its premise must agree on
    what counts as public, or the two drift into deciding different rule sets."""
    return (
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    )


def _unreferenced_primitive_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A public vocabulary primitive that no module outside its own names.

    Restricted to the vocabulary layer by name; sweeping other modules'
    single-consumer symbols and private helpers would make the rule unadoptable."""
    if path.name not in _VOCABULARY_LAYER:
        return []
    outside: set[str] = set()
    for other, names in _reference_index().items():
        if other != path:
            outside |= names
    return [
        offender_at(
            path,
            node,
            f"'{node.name}' is public in the shared vocabulary layer but no module "
            f"outside {path.name} names it: delete it, or mark it private with a "
            "leading underscore so the declaration matches the usage",
        )
        for node in _public_definitions(tree)
        if node.name not in outside
    ]


PLANTED_ORPHAN_PRIMITIVE = "def orphan_walker(tree, path):\n    return []\n"


def _vocabulary_has_public_primitives() -> None:
    """The rule has something to decide, derived from the tree rather than named.

    An emptied or renamed layer would leave the detector matching nothing --
    green while reporting a property; inside the detector ``-O`` removes it."""
    exported = [
        node.name
        for path, tree in _tests_tree(exclude=frozenset())
        if path.name in _VOCABULARY_LAYER
        for node in _public_definitions(tree)
    ]
    if not exported:
        raise AssertionError(
            "the vocabulary layer exports no public primitives, so this rule is deciding "
            "nothing and would report every sweep as clean"
        )


VOCABULARY_PRIMITIVES_ARE_REFERENCED = Rule(
    name="vocabulary-primitives-are-referenced",
    find=_unreferenced_primitive_offenders,
    summary=(
        "a public name in the shared vocabulary layer has at least one caller: an "
        "unreferenced one is dead code or an unmarked private helper, and either way "
        "it is advertised to the next author as the sanctioned spelling"
    ),
    scope=_tests_tree,
    premise=_vocabulary_has_public_primitives,
    bite=lambda: Bites(
        PLANTED_ORPHAN_PRIMITIVE,
        marker="no module outside",
        count=1,
        at=REPO_ROOT / "tests" / "chokepoint_walks.py",
    ),
    clean=lambda: (
        # A leading underscore is the whole declaration, so a private helper at the same path as the offender
        # must be spared.
        Silent(
            "def _mine(tree):\n    return []\n",
            at=REPO_ROOT / "tests" / "rules.py",
        ),
        # And the sweep is a named list, not a directory walk: a fixture factory's public double has one
        # consumer and is not the false memory this catches.
        Silent(
            "def script_walker(tree, path):\n    return []\n",
            at=REPO_ROOT / "tests" / "_fetcher_factories.py",
        ),
    ),
)


# =========================================================================== The retired vocabulary bucket
_RETIRED_VOCABULARY = "app.constants"


_RETIRED_VOCABULARY_IMPORTED = (
    "imports app.constants, the topic bucket whose vocabulary lives in per-topic modules now: "
    "name the topic (app.vocabulary.taxonomy, app.vocabulary.limits, app.vocabulary.identity, ...)"
)


def _retired_vocabulary_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """This module's sites still importing the retired bucket, by line."""
    offenders: list[str] = []
    from_nodes: tuple[ast.ImportFrom, ...] = from_imports(tree)
    plain_nodes: tuple[ast.Import, ...] = plain_imports(tree)
    for node in from_nodes:
        if node.module == _RETIRED_VOCABULARY:
            offenders.append(offender_at(path, node, _RETIRED_VOCABULARY_IMPORTED))
    for plain_node in plain_nodes:
        if any(alias.name == _RETIRED_VOCABULARY for alias in plain_node.names):
            offenders.append(offender_at(path, plain_node, _RETIRED_VOCABULARY_IMPORTED))
    return offenders


# #149's definition of done, as a gate rather than a one-time sweep: a sweep satisfies for one
# commit, this stops the retired bucket regrowing.
RETIRED_VOCABULARY_IS_NOT_IMPORTABLE = Rule(
    name="vocabulary-imports-name-a-topic",
    find=_retired_vocabulary_offenders,
    summary=(
        "app/constants.py was one topic bucket -- taxonomy, strategies, limits, browser vocabulary, "
        "request profiles, host classifiers and identity in a single file, so a reader could not "
        "answer 'where does X live' from the mental model. It is retired: the vocabulary lives one "
        "topic per module under app.vocabulary, so name the topic instead"
    ),
    scope=lambda *, exclude: trees_under(APP_ROOT),
    bite=lambda: Bites(
        # Both spellings, because a fix that repoints `from app.constants import X` and leaves `import
        # app.constants` is a site that still reads as the old layout.
        "from app.constants import UNSORTED_TOPIC_NAME\nimport app.constants as bucket\n",
        marker="name the topic",
        count=2,
    ),
    clean=lambda: (
        # A name that merely ENDS in constants is not the retired module, and neither is a stdlib or
        # third-party import of the same shape.
        "from app.vocabulary.taxonomy import CrawlClassification\n"
        "from app.constants_helpers import helper\n"
        "import constants\nfrom constants import value\n",
    ),
)

RULES = Registry((VOCABULARY_PRIMITIVES_ARE_REFERENCED, RETIRED_VOCABULARY_IS_NOT_IMPORTABLE))


def test_no_rule_violations():
    """Vocabulary primitives are referenced; retired buckets are not imported."""
    RULES.check_all_clean()
