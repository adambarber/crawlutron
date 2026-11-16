"""package_roots() and import_roots() are @cache-wrapped (#1096)."""

from tests._walk_primitives import import_roots, package_roots


def test_import_roots_returns_same_object_twice():
    """Two calls to import_roots() must return the exact same object
    (the @cache wrapper guarantees identity across invocations)."""
    a = import_roots()
    b = import_roots()
    assert a is b, "import_roots() must be @cache-wrapped (different tuple objects returned)"


def test_package_roots_returns_same_object_twice():
    """Two calls to package_roots() must return the exact same object."""
    a = package_roots()
    b = package_roots()
    assert a is b, "package_roots() must be @cache-wrapped (different tuple objects returned)"


def test_import_roots_rglob_called_once_across_two_calls():
    """import_roots() must not re-run Path.rglob on repeated calls: first call
    walks via rglob; with @cache the second returns the cached tuple and triggers
    zero rglob calls."""
    import pathlib
    from unittest.mock import patch

    import_roots.cache_clear()
    package_roots.cache_clear()

    count = 0
    original_rglob = pathlib.Path.rglob

    def count_rglob(self, pattern):
        nonlocal count
        count += 1
        return original_rglob(self, pattern)

    with patch.object(pathlib.Path, "rglob", count_rglob):
        import_roots()
        first = count
    assert first > 0, "rglob should fire on the first call"

    count = 0
    with patch.object(pathlib.Path, "rglob", count_rglob):
        import_roots()
        second = count

    assert second == 0, f"rglob called {second} times on cached import_roots() (expected 0)"
