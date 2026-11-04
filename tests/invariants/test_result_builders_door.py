"""The engine-result door: one builder, one declaring home (#314, 3.3).

The suite builds a fake ``EngineCrawlResult`` through ``engine_result`` on
``tests._result_builders`` and nothing else importable: any second import home
for a builder name, or any new public constructor on the door, is the
harness-sprawl defect this gate names by file and line."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, from_imports, relative_posix, trees_under
from tests.rules import Rule

#: The one door every engine-result builder imports through.
_RESULT_DOOR = "tests._result_builders"

#: The sanctioned importable public surface of the door: the one builder plus
#: the other-concern helpers; a new public name here is a new constructor, refused.
DOOR_SURFACE = frozenset(
    {
        "engine_result",
        "skipped_result",
        "make_job",
        "crawl_result",
        "Spill",
        "GRAPH_ROOT",
        "GRAPH_CHILD",
    }
)

#: Every builder name the chokepoint governs; one declaring home each.
RESULT_BUILDER_NAMES = frozenset(
    {
        "engine_result",
        "skipped_result",
        "_canned_result",
        "_successful_engine_result",
        "make_engine_result",
        "make_result",
        "_failed_result",
        "_flaky",
        "_scripted_result_map",
        "_root_child_results",
        "_root_fans_out",
        "_small_link_graph_results",
    }
)

_TESTS_DIR = REPO_ROOT / "tests"


def _imported_offender_names(
    tree: ast.Module,
    *,
    door: str,
    governed: frozenset[str] | None,
) -> list[tuple[ast.alias, ast.ImportFrom]]:
    """Which ``alias`` off which from-import is this rule's business.

    ``governed`` selects a names-set off modules other than ``door``; ``None``
    selects non-private, non-surface names off ``door`` itself."""
    hits: list[tuple[ast.alias, ast.ImportFrom]] = []
    for node in from_imports(tree):
        if not node.module:
            continue
        wants_door = governed is None
        if wants_door == (node.module == door):
            # governed set: skip the door itself; surface check: only the door.
            for alias in node.names:
                if governed is not None:
                    if alias.name in governed:
                        hits.append((alias, node))
                elif not alias.name.startswith("_") and alias.name not in DOOR_SURFACE:
                    hits.append((alias, node))
    return hits


def _builder_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A governed builder name imported off any module but the door, file and line named."""
    return [
        f"{relative_posix(path)}:{node.lineno}: {alias.name} must import from {_RESULT_DOOR}"
        for alias, node in _imported_offender_names(tree, door=_RESULT_DOOR, governed=RESULT_BUILDER_NAMES)
    ]


def _surface_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A non-private name imported off the door beyond its sanctioned surface."""
    return [
        f"{relative_posix(path)}:{node.lineno}: {alias.name} is outside the sanctioned {_RESULT_DOOR} surface"
        for alias, node in _imported_offender_names(tree, door=_RESULT_DOOR, governed=None)
    ]


RESULT_BUILDERS_IMPORT_ONLY_THROUGH_THE_DOOR = Rule(
    name="result-builders-import-only-through-the-door",
    find=_builder_offenders,
    summary=(
        "imports an engine-result builder from a module other than "
        "tests._result_builders; the door owns the construction, so a new file "
        "importing from elsewhere is the harness-sprawl defect the chokepoint "
        "exists to make obvious"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        # A moved-away sibling module is still a second home.
        "from tests._result_profiles import engine_result\n",
        # A re-export shim passing the name along is the same weld.
        "from tests._builder_shims import _canned_result\n",
        # A builder imported off an unrelated helper module is no better.
        "from tests._crawl_runners import make_engine_result\n",
    ),
    clean=lambda: (
        # The door is the whole point: any governed name may import from it.
        "from tests._result_builders import engine_result\n",
        "from tests._result_builders import skipped_result, make_job\n",
        # A name the door does not govern is not this rule's business.
        "from tests._crawl_runners import crawled_results\n",
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)

DOOR_SURFACE_IS_EXACTLY_THE_ONE_BUILDER = Rule(
    name="result-builders-door-surface-is-exactly-the-one-builder",
    find=_surface_offenders,
    summary=(
        "imports a public name off tests._result_builders beyond its sanctioned "
        "surface; the door owns one EngineCrawlResult builder, so a new public "
        "constructor on the door is forbidden -- shape the call at the site"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        # A deleted thin profile, back as an import.
        "from tests._result_builders import make_engine_result\n",
        # A brand-new public constructor on the door.
        "from tests._result_builders import canned_result_v2\n",
    ),
    clean=lambda: (
        "from tests._result_builders import engine_result\n",
        "from tests._result_builders import Spill, GRAPH_ROOT, GRAPH_CHILD\n",
        "from tests._result_builders import skipped_result, crawl_result, make_job\n",
        # A private name on the door is allowed, not a public constructor.
        "from tests._result_builders import _link\n",
        "import tests._result_builders as door\nbuild = door.engine_result\n",
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)

RULES = (RESULT_BUILDERS_IMPORT_ONLY_THROUGH_THE_DOOR, DOOR_SURFACE_IS_EXACTLY_THE_ONE_BUILDER)


def test_result_builders_import_only_through_the_door():
    RESULT_BUILDERS_IMPORT_ONLY_THROUGH_THE_DOOR.scan()


def test_result_builders_door_surface_is_exactly_the_one_builder():
    DOOR_SURFACE_IS_EXACTLY_THE_ONE_BUILDER.scan()
