"""Every browser double imports only through ``tests._browser_doubles``: the browser family had four import
homes (#844); the consolidation made one door with one page-fake and one pool-fake implementation behind
it. A from-import off an implementation module is the second altitude the door exists to refuse.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, from_imports, relative_posix, trees_under
from tests.rules import Rule

_BROWSER_DOOR = "tests._browser_doubles"

#: The implementation homes the door re-exports; importing them directly is
#: the drift the consolidation closed.
_IMPLEMENTATION_HOMES = frozenset(
    {
        "tests._browser_page_doubles",
        "tests._browser_pool_doubles",
        "tests._pool_ws_doubles",
    }
)

_TEST_DIR = REPO_ROOT / "tests"


def _browser_double_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A browser double imported off the door, file and line named."""
    out: list[str] = []
    for node in from_imports(tree):
        if node.module not in _IMPLEMENTATION_HOMES:
            continue
        names = ", ".join(alias.name for alias in node.names)
        out.append(
            f"{relative_posix(path)}:{node.lineno}: {names} must import from "
            f"{_BROWSER_DOOR}, not {node.module}"
        )
    return out


BROWSER_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR = Rule(
    name="browser-doubles-import-only-through-the-door",
    find=_browser_double_offenders,
    summary=(
        "imports a browser double from an implementation module instead of the "
        "tests._browser_doubles door; one page-fake and one pool-fake live "
        "behind the door, so a direct implementation import is the sprawl #844 "
        "consolidated"
    ),
    scope=lambda *, exclude: trees_under(_TEST_DIR),
    # The door itself re-exports the implementations; its own body is not a caller.
    skip=frozenset({"tests/_browser_doubles.py"}),
    bite=lambda: (
        "from tests._browser_page_doubles import ScriptedPage\n",
        "from tests._pool_ws_doubles import FakeWS\n",
        "from tests._browser_pool_doubles import SpyRenderer, close_mid_sweep\n",
    ),
    clean=lambda: (
        "from tests._browser_doubles import ScriptedPage, FakeWS, SpyRenderer\n",
        "from tests._browser_doubles import _fake_browser, render_pinned\n",
        # Unrelated helper imports are not this rule's business.
        "from tests._policy_factories import canonical\n",
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)


def test_browser_doubles_import_only_through_the_door():
    BROWSER_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR.scan()
