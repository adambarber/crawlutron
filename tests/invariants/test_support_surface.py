"""The suite imports shared test helpers only through the declared support surface.

The private-import gate (#476) keeps app/ honest; this gate keeps the suite
itself honest: a ``test_*`` module's internals are that test's own seam, and
shared machinery lives on ``tests/_*.py`` / ``tests/invariants/_*.py``.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import relative_posix, trees_under
from tests.invariants._shared import _private_from_import_names
from tests.rules import Registry, Rule

_TESTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests"


def _cross_test_private_names(tree: ast.Module, path: pathlib.Path) -> dict[str, int]:
    """``module.name -> lineno`` for private names imported from sibling test modules.

    A ``test_*`` module's internals are that test's own seam; a shared helper
    lives on the declared surface (``tests/_*.py``, ``tests/factories/``,
    ``tests/invariants/_*.py``), so reaching into a sibling test module is the
    weld this rule refuses."""
    here = relative_posix(path) if path.is_absolute() else path.as_posix()
    return _private_from_import_names(
        tree,
        lambda module: module.startswith("tests") and module.rsplit(".", 1)[-1].startswith("test_"),
        here,
    )


def _cross_test_private_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    return [
        f"{relative}:{lineno}: {key} is a private name in a sibling test module; move the "
        "helper to the declared support surface (tests/_*.py or tests/invariants/_*.py) "
        "and import it from there"
        for key, lineno in sorted(_cross_test_private_names(tree, path).items())
    ]


SUPPORT_SURFACE_LEDGER = Rule(
    name="the-suite-imports-shared-test-helpers-from-the-support-surface",
    find=_cross_test_private_offenders,
    summary=(
        "imports a private helper out of a sibling test module's internals; shared test "
        "machinery lives on the declared support surface (tests/_*.py, tests/factories/, "
        "tests/invariants/_*), so the import names that surface instead"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        # The direct weld: one test module reaching another test module's helper.
        "from tests.invariants.test_registry import _property\n",
        # A package-qualified sibling is the same reach through a longer spelling.
        "from tests.api.test_store_offload import _ROOT\n",
    ),
    clean=lambda: (
        # The support surface is the whole point: same private spelling, declared home.
        "from tests.invariants._registry_machinery import _property\n",
        "from tests._ci_contract import _hook_blocks\n",
        # A non-test target is somebody else's rule.
        "from app.transport.scheduler import DomainScheduler\n",
        # A module-level test-module import without a private name is not a helper reach.
        "from tests import membership_gate\n",
    ),
)

SURFACE_RULES = Registry((SUPPORT_SURFACE_LEDGER,))


def test_support_surface_no_rule_violations():
    SURFACE_RULES.check_all_clean()
