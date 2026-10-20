"""Structural marker for the invariant layer (#252).

Every test under tests/invariants/ asserts on app/ source SYNTAX, not runtime
behavior, so the package is the `structural` set and `-m structural` reselects it.
"""

import pytest


def pytest_collection_modifyitems(session, config, items):
    """Mark every collected invariant test with ``structural``."""
    for item in items:
        item.add_marker(pytest.mark.structural)
