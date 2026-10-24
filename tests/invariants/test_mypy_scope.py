"""The mypy scope covers every shared-harness module it claims (#397)."""

from __future__ import annotations

import configparser

from tests.chokepoint_walks import REPO_ROOT

_MYPY_INI = REPO_ROOT / "mypy.ini"
_SCOPE_GLOB = "tests/_*.py"


def _declared_scope_files() -> str:
    """The value of mypy.ini's ``files`` key, read as INI so the key is named."""
    parser = configparser.ConfigParser()
    parser.read(_MYPY_INI)
    return parser["mypy"]["files"]


def test_the_mypy_scope_names_the_harness_glob() -> None:
    """mypy.ini must scope ``tests/_*.py`` as a glob, not two named modules."""
    assert _SCOPE_GLOB in _declared_scope_files(), (
        "mypy.ini files = "
        + _declared_scope_files()
        + " does not contain the "
        + _SCOPE_GLOB
        + " glob; a harness module is then checked only by transitive-import luck"
    )


def test_the_scope_covers_the_real_harness_tree() -> None:
    """The glob is not vestigial: the tree must actually hold harness modules."""
    assert (REPO_ROOT / "tests" / "_policy_factories.py").exists(), (
        "the reach proof's module is gone; re-ground this gate in the live tree"
    )


def test_the_old_two_module_scope_fails_the_gate() -> None:
    """The pre-#397 scope (named modules, no glob) fails the invariant."""
    old_scope = "app, tests/_fetch_outcome.py, tests/_transport_doubles.py"
    assert _SCOPE_GLOB not in old_scope, "the old scope must not contain the glob"
