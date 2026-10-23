"""The ``factory`` fixture: one handle over tests/factories."""

import pytest

from tests.factories import Factory


@pytest.fixture
def factory(tmp_path):
    """The construction surface: ``factory.app()``, ``factory.client()``,
    ``factory.db()``, and ``factory.cookie_jar()``."""
    return Factory(tmp_path)
