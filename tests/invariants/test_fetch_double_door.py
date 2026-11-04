"""Every fetch double imports only through ``tests._fetch_doubles`` (#314, 3.3).

One declaring home per name: a fetch-double name imports only from the door, and
the door's public surface is exactly the two constructions plus the route
grammar. A re-export shim, a sibling module, or a new public constructor on the
door is the harness-sprawl defect the chokepoint exists to make obvious."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, from_imports, relative_posix, trees_under
from tests.rules import Rule

#: The one door every fetch double imports through.
_FETCH_DOUBLES_DOOR = "tests._fetch_doubles"

#: The sanctioned importable public surface of the door: the two constructions
#: plus the route grammar. A new public name here is a new constructor, refused.
DOOR_SURFACE = frozenset({"make_fetch", "FetcherDouble", "Route", "PageData", "PageMap"})

#: Every fetch-double name the chokepoint governs; one declaring home each.
FETCH_DOUBLE_NAMES = frozenset(
    {
        "make_fetch",
        "FetcherDouble",
        "scripted_fetcher",
        "routed_fetcher",
        "tap_fetcher",
        "request_fetcher",
        "sequenced_fetcher",
        "concurrent_fetcher",
        "hanging_fetcher",
        "site_fetcher",
        "ok_fetcher",
        "raise_fetcher",
        "failing_fetcher",
        "answer",
        "Route",
        "PageData",
        "PageMap",
    }
)

_TESTS_DIR = REPO_ROOT / "tests"


def _fetch_double_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A fetch-double name imported from anywhere but the door, file and line named.

    ``tests._fetch_doubles`` is the single import door; a from-import of a
    governed name off that module is a second home for one declaration."""
    out: list[str] = []
    for node in from_imports(tree):
        if not node.module or node.module == _FETCH_DOUBLES_DOOR:
            continue
        for alias in node.names:
            if alias.name not in FETCH_DOUBLE_NAMES:
                continue
            out.append(
                f"{relative_posix(path)}:{node.lineno}: {alias.name} must import from "
                f"{_FETCH_DOUBLES_DOOR}, not {node.module}"
            )
    return out


def _surface_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A public name off the door outside its sanctioned surface, file and line named.

    The door owns exactly two constructions and the route grammar; a public name
    beyond that set is a new fetch constructor, which is the proliferation the
    chokepoint forbids."""
    out: list[str] = []
    for node in from_imports(tree):
        if node.module != _FETCH_DOUBLES_DOOR:
            continue
        for alias in node.names:
            if alias.name.startswith("_") or alias.name in DOOR_SURFACE:
                continue
            out.append(
                f"{relative_posix(path)}:{node.lineno}: {alias.name} is outside the "
                f"sanctioned {_FETCH_DOUBLES_DOOR} surface {sorted(DOOR_SURFACE)}"
            )
    return out


FETCH_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR = Rule(
    name="fetch-doubles-import-only-through-the-door",
    find=_fetch_double_offenders,
    summary=(
        "imports a fetch double from a module other than tests._fetch_doubles; the "
        "door owns the behavior, so a new file importing from elsewhere is the "
        "harness-sprawl defect the chokepoint exists to make obvious"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        # A moved-away sibling module is still a second home.
        "from tests._fetch_scripted import routed_fetcher\n",
        # A re-export shim passing the name along is the same weld.
        "from tests._fetcher_factories import make_fetch\n",
        # A fetch double imported off an unrelated helper module is no better.
        "from tests._crawl_runners import tap_fetcher\n",
    ),
    clean=lambda: (
        # The door is the whole point: any governed name may import from it.
        "from tests._fetch_doubles import scripted_fetcher\n",
        "from tests._fetch_doubles import FetcherDouble, routed_fetcher\n",
        # A name the door does not govern is not this rule's business.
        "from tests._fetcher_factories import run_http_strategy\n",
        "from tests._crawl_runners import crawled_results\n",
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)

DOOR_SURFACE_IS_EXACTLY_THE_TWO_CONSTRUCTIONS = Rule(
    name="fetch-doubles-door-surface-is-exactly-the-two-constructions",
    find=_surface_offenders,
    summary=(
        "imports a public name off tests._fetch_doubles beyond its sanctioned "
        "surface; a new public constructor on the door is forbidden, so the small "
        "answer closure belongs at the site"
    ),
    scope=lambda *, exclude: trees_under(_TESTS_DIR),
    bite=lambda: (
        "from tests._fetch_doubles import scripted_fetcher\n",
        "from tests._fetch_doubles import run_scoped_crawl\n",
    ),
    clean=lambda: (
        "from tests._fetch_doubles import make_fetch\n",
        "from tests._fetch_doubles import FetcherDouble, Route, PageData, PageMap\n",
        # A private name on the door is allowed, not a constructor.
        "from tests._fetch_doubles import _answer\n",
        "import tests._fetch_doubles as door\nru = door.make_fetch\n",
    ),
    proof_path=REPO_ROOT / "tests" / "_synthetic.py",
)

RULES = (FETCH_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR, DOOR_SURFACE_IS_EXACTLY_THE_TWO_CONSTRUCTIONS)


def test_fetch_doubles_import_only_through_the_door():
    FETCH_DOUBLES_IMPORT_ONLY_THROUGH_THE_DOOR.scan()


def test_fetch_doubles_door_surface_is_exactly_the_two_constructions():
    DOOR_SURFACE_IS_EXACTLY_THE_TWO_CONSTRUCTIONS.scan()
