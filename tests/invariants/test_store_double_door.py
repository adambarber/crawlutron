"""Every loop-guard store double imports only through ``tests._job_store_doubles``.

``tests.api._offload_doubles`` was a second home for the same store seams
(#844); the consolidation made ``tests._job_store_doubles`` the one declaring
home, so a from-import off the old module is the drift the door refuses.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, from_imports, relative_posix, trees_under
from tests.rules import Rule

_STORE_DOUBLES_DOOR = "tests._job_store_doubles"

#: The old second home, deleted by #844; any surviving import is a stale repoint.
_RETIRED_HOME = "tests.api._offload_doubles"

_LOOP_GUARD_NAMES = frozenset(
    {
        "LoopGuardJobStore",
        "LoopGuardResultStore",
        "LoopGuardWatchStore",
        "LoopGuardSharedCookieStore",
        "_off_loop",
        "_refuse_on_loop",
    }
)

_TEST_DIR = REPO_ROOT / "tests"


def _store_double_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A loop-guard double imported off the door, file and line named."""
    out: list[str] = []
    for node in from_imports(tree):
        if node.module != _STORE_DOUBLES_DOOR:
            for alias in node.names:
                if alias.name in _LOOP_GUARD_NAMES and node.module not in (None, _STORE_DOUBLES_DOOR):
                    out.append(
                        f"{relative_posix(path)}:{node.lineno}: {alias.name} must import "
                        f"from {_STORE_DOUBLES_DOOR}, not {node.module}"
                    )
        if node.module == _RETIRED_HOME:
            out.append(
                f"{relative_posix(path)}:{node.lineno}: {_RETIRED_HOME} is retired; "
                f"import the loop-guard doubles from {_STORE_DOUBLES_DOOR}"
            )
    return out


STORE_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR = Rule(
    name="store-doubles-import-only-through-the-door",
    find=_store_double_offenders,
    summary=(
        "imports a loop-guard store double from anywhere but "
        "tests._job_store_doubles; the offload module retired, so a second "
        "home for these doubles is the drift #844 consolidated"
    ),
    scope=lambda *, exclude: trees_under(_TEST_DIR),
    bite=lambda: (
        "from tests.api._offload_doubles import LoopGuardJobStore\n",
        "from tests.api._loop_guard import _off_loop\n",
    ),
    clean=lambda: (
        "from tests._job_store_doubles import LoopGuardJobStore, _off_loop\n",
        "from tests._job_store_doubles import LoopGuardSharedCookieStore\n",
        # An unrelated name off another helper is not this rule's business.
        "from tests._policy_factories import job_store\n",
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


def test_store_doubles_import_only_through_the_door():
    STORE_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR.scan()
