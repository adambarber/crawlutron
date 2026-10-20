"""Structural marker for the invariant layer (#252).

Every test under tests/invariants/ asserts on app/ source SYNTAX, not runtime
behavior, so the package is the `structural` set and `-m structural` reselects it.
"""

import pytest

_INVARIANTS_DIR = "tests/invariants"


def pytest_collection_modifyitems(session, config, items):
    """Mark every test under tests/invariants/ with ``structural``.

    The hook is global, not subtree-scoped, so guard on the item's own path to
    avoid stamping -- and silently deselecting -- the whole suite.
    """
    for item in items:
        if str(item.fspath).replace(chr(92), "/") and _INVARIANTS_DIR in str(item.fspath).replace(
            chr(92), "/"
        ):
            item.add_marker(pytest.mark.structural)
